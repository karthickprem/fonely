"""Tenant-scoped persistence for the durable WhatsApp inbound queue."""

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


def _next_attempt_at(failed_attempts: int) -> datetime:
    index = min(max(failed_attempts - 1, 0), len(BACKOFF_SECONDS) - 1)
    return datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[index])


def deterministic_lock_key(business_id: int, sender_phone: str) -> int:
    data = f"{business_id}:{sender_phone}".encode()
    digest = hashlib.blake2b(data, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class InboundEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_next_eligible(self) -> WhatsAppInboundEvent | None:
        """Claim one conversation head, including expired processing leases."""
        result = await self._session.execute(
            text(
                "SELECT e.id FROM whatsapp_inbound_events e "
                "WHERE ("
                "  (e.status = ANY(:ready_statuses) "
                "   AND (e.next_attempt_at <= NOW() OR e.next_attempt_at IS NULL)) "
                "  OR (e.status = 'processing' AND e.lease_expires_at < NOW())"
                ") "
                "AND e.attempts < e.max_attempts "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM whatsapp_inbound_events earlier "
                "  WHERE earlier.business_id = e.business_id "
                "    AND earlier.sender_phone = e.sender_phone "
                "    AND earlier.status != ALL(:terminal_statuses) "
                "    AND (earlier.provider_timestamp, earlier.id) "
                "        < (e.provider_timestamp, e.id)"
                ") "
                "ORDER BY e.provider_timestamp, e.id "
                "LIMIT 1 FOR UPDATE OF e SKIP LOCKED"
            ),
            {
                "ready_statuses": [
                    InboundEventStatus.RECEIVED.value,
                    InboundEventStatus.FAILED.value,
                ],
                "terminal_statuses": list(_TERMINAL_STATUSES),
            },
        )
        event_id = result.scalar_one_or_none()
        if event_id is None:
            return None
        event = await self._session.get(WhatsAppInboundEvent, event_id)
        if event is None:  # pragma: no cover - locked row cannot disappear
            return None

        now = datetime.now(UTC)
        event.status = InboundEventStatus.PROCESSING.value
        event.claim_token = uuid.uuid4()
        event.claimed_at = now
        event.lease_expires_at = now + LEASE_DURATION
        event.claim_version += 1
        await self._session.flush()
        return event

    async def require_owned_claim(
        self,
        business_id: int,
        event_id: int,
        claim_token: uuid.UUID,
        claim_version: int,
    ) -> WhatsAppInboundEvent:
        event = await self._session.scalar(
            select(WhatsAppInboundEvent).where(
                WhatsAppInboundEvent.business_id == business_id,
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.status == InboundEventStatus.PROCESSING.value,
                WhatsAppInboundEvent.claim_token == claim_token,
                WhatsAppInboundEvent.claim_version == claim_version,
                WhatsAppInboundEvent.lease_expires_at > datetime.now(UTC),
            )
        )
        if event is None:
            raise StaleClaimError(f"event {event_id}: stale or expired claim")
        return event

    async def verify_and_mark_domain_processed(
        self,
        business_id: int,
        event_id: int,
        claim_token: uuid.UUID,
        claim_version: int,
    ) -> None:
        result = await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.business_id == business_id,
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.status == InboundEventStatus.PROCESSING.value,
                WhatsAppInboundEvent.claim_token == claim_token,
                WhatsAppInboundEvent.claim_version == claim_version,
            )
            .values(
                status=InboundEventStatus.DOMAIN_PROCESSED.value,
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
            )
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise StaleClaimError(f"event {event_id}: claim ownership changed")

    async def mark_failed(
        self,
        business_id: int,
        event_id: int,
        claim_token: uuid.UUID,
        claim_version: int,
        error_class: str,
    ) -> bool:
        event = await self._session.scalar(
            select(WhatsAppInboundEvent).where(
                WhatsAppInboundEvent.business_id == business_id,
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.status == InboundEventStatus.PROCESSING.value,
                WhatsAppInboundEvent.claim_token == claim_token,
                WhatsAppInboundEvent.claim_version == claim_version,
            )
        )
        if event is None:
            return False

        attempts = event.attempts + 1
        dead = attempts >= event.max_attempts
        values: dict[str, object] = {
            "status": (
                InboundEventStatus.DEAD_LETTER.value if dead else InboundEventStatus.FAILED.value
            ),
            "attempts": attempts,
            "last_error": error_class[:500],
            "next_attempt_at": None if dead else _next_attempt_at(attempts),
            "claim_token": None,
            "claimed_at": None,
            "lease_expires_at": None,
        }
        if dead:
            values["dead_lettered_at"] = datetime.now(UTC)
        await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.business_id == business_id,
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.status == InboundEventStatus.PROCESSING.value,
                WhatsAppInboundEvent.claim_token == claim_token,
                WhatsAppInboundEvent.claim_version == claim_version,
            )
            .values(**values)
        )
        return True

    async def mark_completed(self, business_id: int, event_id: int, completed_at: datetime) -> None:
        result = await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.business_id == business_id,
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.status == InboundEventStatus.DOMAIN_PROCESSED.value,
            )
            .values(
                status=InboundEventStatus.COMPLETED.value,
                completed_at=completed_at,
                message_body=None,
            )
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise StaleClaimError(f"event {event_id}: not domain_processed")

    async def mark_response_failed(self, business_id: int, event_id: int) -> None:
        await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.business_id == business_id,
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.status == InboundEventStatus.DOMAIN_PROCESSED.value,
            )
            .values(
                status=InboundEventStatus.RESPONSE_FAILED.value,
                dead_lettered_at=datetime.now(UTC),
            )
        )

    async def acquire_conversation_lock(self, business_id: int, sender_phone: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": deterministic_lock_key(business_id, sender_phone)},
        )
