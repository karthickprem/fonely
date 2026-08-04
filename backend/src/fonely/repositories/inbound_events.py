"""Tenant-scoped repository for WhatsApp inbound event queue.

Implements head-of-line ordering per conversation, claim ownership
with tokens, and deterministic cross-process advisory lock keys.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.enums import InboundEventStatus
from fonely.models.schema import WhatsAppInboundEvent

BACKOFF_SECONDS = (30, 60, 120, 300, 600)
LEASE_DURATION = timedelta(minutes=5)

_TERMINAL_STATUSES = (
    InboundEventStatus.COMPLETED.value,
    InboundEventStatus.DEAD_LETTER.value,
    InboundEventStatus.RESPONSE_FAILED.value,
)


class StaleClaimError(Exception):
    pass


def _next_attempt_at(attempts: int) -> datetime:
    index = min(attempts, len(BACKOFF_SECONDS) - 1)
    return datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[index])


def deterministic_lock_key(business_id: int, sender_phone: str) -> int:
    """Stable advisory lock key — identical across processes regardless of PYTHONHASHSEED."""
    data = f"{business_id}:{sender_phone}".encode()
    digest = hashlib.blake2b(data, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class InboundEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_next_eligible(self, now: datetime | None = None) -> WhatsAppInboundEvent | None:
        """Claim the oldest eligible event respecting per-conversation ordering.

        Head-of-line: a later event for the same (business_id, sender_phone)
        is not eligible while an earlier nonterminal event exists.
        Uses raw SQL for the correlated NOT EXISTS subquery.
        """
        result = await self._session.execute(
            text(
                "SELECT e.* FROM whatsapp_inbound_events e "
                "WHERE e.status = ANY(:eligible_statuses) "
                "  AND e.attempts < e.max_attempts "
                "  AND (e.next_attempt_at <= NOW() OR e.next_attempt_at IS NULL) "
                "  AND NOT EXISTS ( "
                "    SELECT 1 FROM whatsapp_inbound_events earlier "
                "    WHERE earlier.business_id = e.business_id "
                "      AND earlier.sender_phone = e.sender_phone "
                "      AND earlier.status != ALL(:terminal_statuses) "
                "      AND earlier.id < e.id "
                "  ) "
                "ORDER BY e.created_at "
                "LIMIT 1 "
                "FOR UPDATE OF e SKIP LOCKED"
            ),
            {
                "eligible_statuses": [
                    InboundEventStatus.RECEIVED.value,
                    InboundEventStatus.FAILED.value,
                ],
                "terminal_statuses": list(_TERMINAL_STATUSES),
            },
        )
        row = result.mappings().first()
        if row is None:
            return None
        event = await self._session.get(WhatsAppInboundEvent, row["id"])
        if event is None:
            return None

        token = uuid.uuid4()
        now_ts = datetime.now(UTC)
        event.status = InboundEventStatus.PROCESSING.value
        event.claim_token = token
        event.claimed_at = now_ts
        event.lease_expires_at = now_ts + LEASE_DURATION
        event.claim_version = (event.claim_version or 0) + 1
        await self._session.flush()
        return event

    async def verify_and_mark_domain_processed(
        self,
        business_id: int,
        event_id: int,
        claim_token: uuid.UUID,
    ) -> None:
        result = await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.business_id == business_id,
                WhatsAppInboundEvent.claim_token == claim_token,
                WhatsAppInboundEvent.status == InboundEventStatus.PROCESSING.value,
            )
            .values(status=InboundEventStatus.DOMAIN_PROCESSED.value)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise StaleClaimError(f"event {event_id}: claim token mismatch or status changed")

    async def mark_completed(self, business_id: int, event_id: int, completed_at: datetime) -> None:
        await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.business_id == business_id,
            )
            .values(
                status=InboundEventStatus.COMPLETED.value,
                completed_at=completed_at,
                message_body=None,
            )
        )

    async def mark_failed(self, business_id: int, event_id: int, error: str) -> None:
        event = await self._session.scalar(
            select(WhatsAppInboundEvent).where(
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.business_id == business_id,
            )
        )
        if event is None:
            return
        new_attempts = event.attempts + 1
        is_dead = new_attempts >= event.max_attempts
        new_status = (
            InboundEventStatus.DEAD_LETTER.value if is_dead else InboundEventStatus.FAILED.value
        )
        values: dict[str, object] = {
            "status": new_status,
            "attempts": new_attempts,
            "last_error": error[:500],
            "next_attempt_at": _next_attempt_at(new_attempts - 1),
        }
        if is_dead:
            values["dead_lettered_at"] = datetime.now(UTC)
        await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.business_id == business_id,
            )
            .values(**values)
        )

    async def mark_response_failed(self, business_id: int, event_id: int) -> None:
        await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.business_id == business_id,
            )
            .values(
                status=InboundEventStatus.RESPONSE_FAILED.value,
                dead_lettered_at=datetime.now(UTC),
            )
        )

    async def acquire_conversation_lock(self, business_id: int, sender_phone: str) -> None:
        key = deterministic_lock_key(business_id, sender_phone)
        await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
