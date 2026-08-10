"""Durable Exotel inbound event persistence.

Implements InboundCallEventIntake for production use. Requires the
exotel_inbound_events table (migration deferred until Dev3 0015
integrates and head is known).

Until the migration is applied, only the InMemoryCallEventIntake test
double should be used. This module imports no ORM model — it uses raw
SQL against the schema defined in EXOTEL_MIGRATION_WORKER_DESIGN.md.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.calls.events import ExotelCallbackEvent
from fonely.domain.calls.intake import (
    DuplicateCallEventError,
    InboundCallEventRecord,
)


class ExotelInboundEventRepository:
    """Production InboundCallEventIntake backed by exotel_inbound_events.

    Semantic idempotency on (business_id, call_sid, event_type).
    Does NOT mutate domain state — stores the raw inbound event only.
    A separate worker claims and processes events.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist(
        self,
        business_id: int,
        event: ExotelCallbackEvent,
    ) -> InboundCallEventRecord:
        payload = json.dumps(
            {
                "call_sid": event.call_sid,
                "event_type": event.event_type,
                "status": event.status,
                "direction": event.direction,
                "duration": event.duration,
                "conversation_duration": event.conversation_duration,
                "custom_field": event.custom_field,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()[:32]

        try:
            result = await self._session.execute(
                text(
                    "INSERT INTO exotel_inbound_events "
                    "(call_sid, business_id, event_type, status, "
                    " caller_phone, called_number, duration, "
                    " conversation_duration, direction, custom_field, "
                    " payload_digest, received_at) "
                    "VALUES (:call_sid, :bid, :etype, :status, "
                    " :caller, :called, :dur, :conv_dur, :dir, "
                    " :custom, :digest, :now) "
                    "RETURNING id"
                ),
                {
                    "call_sid": event.call_sid,
                    "bid": business_id,
                    "etype": event.event_type,
                    "status": event.status,
                    "caller": event.caller_phone,
                    "called": event.called_number,
                    "dur": event.duration,
                    "conv_dur": event.conversation_duration,
                    "dir": event.direction,
                    "custom": event.custom_field,
                    "digest": digest,
                    "now": datetime.now(UTC),
                },
            )
            row_id = result.scalar_one()
        except Exception as exc:
            if "uq_exotel_inbound_call_event" in str(exc):
                raise DuplicateCallEventError(
                    f"duplicate: {event.call_sid}/{event.event_type}"
                ) from exc
            raise

        await self._session.flush()

        return InboundCallEventRecord(
            id=row_id,
            business_id=business_id,
            call_sid=event.call_sid,
            event_type=event.event_type,
            status=event.status,
            caller_phone=event.caller_phone,
            called_number=event.called_number,
            duration=event.duration,
            direction=event.direction,
            payload_digest=digest,
        )

    async def claim_next_eligible(self) -> dict | None:
        """Claim one eligible event for processing.

        Returns a dict with event fields and claim metadata, or None.
        Uses SELECT FOR UPDATE SKIP LOCKED for concurrent safety.
        """
        result = await self._session.execute(
            text(
                "SELECT id, call_sid, business_id, event_type, status, "
                "  caller_phone, called_number, duration, direction, "
                "  claim_version "
                "FROM exotel_inbound_events "
                "WHERE ("
                "  (intake_status IN ('received', 'failed') "
                "   AND (next_attempt_at <= NOW() OR next_attempt_at IS NULL))"
                "  OR (intake_status = 'processing' AND lease_expires_at < NOW())"
                ") "
                "AND attempts < max_attempts "
                "ORDER BY received_at "
                "LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
        )
        row = result.one_or_none()
        if row is None:
            return None

        import uuid

        claim_token = str(uuid.uuid4())
        new_version = row[9] + 1
        await self._session.execute(
            text(
                "UPDATE exotel_inbound_events SET "
                "  intake_status = 'processing', "
                "  claim_token = :token, "
                "  claim_version = :version, "
                "  claimed_at = NOW(), "
                "  lease_expires_at = NOW() + INTERVAL '5 minutes', "
                "  attempts = attempts + 1 "
                "WHERE id = :eid AND claim_version = :old_version"
            ),
            {
                "token": claim_token,
                "version": new_version,
                "eid": row[0],
                "old_version": row[9],
            },
        )
        await self._session.flush()

        return {
            "id": row[0],
            "call_sid": row[1],
            "business_id": row[2],
            "event_type": row[3],
            "status": row[4],
            "caller_phone": row[5],
            "called_number": row[6],
            "duration": row[7],
            "direction": row[8],
            "claim_token": claim_token,
            "claim_version": new_version,
        }

    async def mark_completed(self, event_id: int, claim_token: str, claim_version: int) -> bool:
        """Mark event as completed after successful domain processing."""
        result = await self._session.execute(
            text(
                "UPDATE exotel_inbound_events SET "
                "  intake_status = 'completed', "
                "  completed_at = NOW(), "
                "  claim_token = NULL, "
                "  claimed_at = NULL, "
                "  lease_expires_at = NULL "
                "WHERE id = :eid "
                "  AND claim_token = :token "
                "  AND claim_version = :version "
                "  AND intake_status = 'processing'"
            ),
            {"eid": event_id, "token": claim_token, "version": claim_version},
        )
        return result.rowcount > 0  # type: ignore[union-attr]

    async def mark_failed(self, event_id: int, claim_token: str, claim_version: int) -> bool:
        """Mark event as failed with backoff; dead-letter after max attempts."""
        backoff_seconds = [30, 60, 120, 300, 600]
        result = await self._session.execute(
            text(
                "UPDATE exotel_inbound_events SET "
                "  intake_status = CASE "
                "    WHEN attempts >= max_attempts THEN 'dead_letter' "
                "    ELSE 'failed' "
                "  END, "
                "  next_attempt_at = CASE "
                "    WHEN attempts >= max_attempts THEN NULL "
                "    ELSE NOW() + make_interval(secs => :backoff) "
                "  END, "
                "  dead_lettered_at = CASE "
                "    WHEN attempts >= max_attempts THEN NOW() "
                "    ELSE NULL "
                "  END, "
                "  claim_token = NULL, "
                "  claimed_at = NULL, "
                "  lease_expires_at = NULL "
                "WHERE id = :eid "
                "  AND claim_token = :token "
                "  AND claim_version = :version "
                "  AND intake_status = 'processing'"
            ),
            {
                "eid": event_id,
                "token": claim_token,
                "version": claim_version,
                "backoff": backoff_seconds[0],
            },
        )
        return result.rowcount > 0  # type: ignore[union-attr]
