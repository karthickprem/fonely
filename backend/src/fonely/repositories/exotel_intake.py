"""Durable Exotel inbound event persistence — caller-owned transactions.

Follows InboundEventRepository pattern. Requires exotel_inbound_events
table (migration deferred until Dev3 0015 integrates).
Feature-disabled until migration is applied.

IMPORTANT: This repository flushes only. The caller owns commit/rollback.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.calls.intake import (
    ClaimedCallEvent,
    ConflictingCallEventError,
    DuplicateCallEventError,
    InboundCallEvent,
    InboundCallEventRecord,
    canonical_event_digest,
)

BACKOFF_SECONDS = (30, 60, 120, 300, 600)


class ExotelInboundEventRepository:
    """Production InboundCallEventIntake. Caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist(
        self,
        business_id: int,
        event: InboundCallEvent,
    ) -> InboundCallEventRecord:
        digest = canonical_event_digest(event)
        now = datetime.now(UTC)

        # Concurrency-safe: INSERT with ON CONFLICT on the unique constraint
        # (business_id, call_sid, event_type). The DB is the arbiter — no
        # SELECT-then-INSERT race. On conflict, returns the existing row's
        # digest for duplicate vs conflict classification.
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
                "ON CONFLICT (business_id, call_sid, event_type) "
                "DO UPDATE SET id = exotel_inbound_events.id "
                "RETURNING id, "
                "  (xmax = 0) AS inserted, "
                "  payload_digest"
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
                "now": now,
            },
        )
        row = result.one()
        row_id, inserted, existing_digest = row[0], row[1], row[2]

        if not inserted:
            if existing_digest == digest:
                raise DuplicateCallEventError(
                    f"exact duplicate: {event.call_sid}/{event.event_type}"
                )
            raise ConflictingCallEventError(
                f"conflicting: {event.call_sid}/{event.event_type}"
            )

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
            conversation_duration=event.conversation_duration,
            direction=event.direction,
            custom_field=event.custom_field,
            payload_digest=digest,
        )

    async def claim_next_eligible(self) -> ClaimedCallEvent | None:
        """Claim one eligible event across all tenants. Caller commits.

        The UPDATE is tenant-scoped via business_id from the SELECTed row.
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

        row_business_id = row[2]
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
                "WHERE id = :eid AND business_id = :bid "
                "  AND claim_version = :old_version"
            ),
            {
                "token": claim_token,
                "version": new_version,
                "eid": row[0],
                "bid": row_business_id,
                "old_version": row[9],
            },
        )
        await self._session.flush()

        return ClaimedCallEvent(
            id=row[0],
            call_sid=row[1],
            business_id=row[2],
            event_type=row[3],
            status=row[4],
            caller_phone=row[5],
            called_number=row[6],
            duration=row[7],
            direction=row[8],
            claim_token=claim_token,
            claim_version=new_version,
        )

    async def mark_completed(
        self, event_id: int, business_id: int, claim_token: str, claim_version: int
    ) -> bool:
        """Mark completed. Returns False if claim is stale. Caller commits."""
        result = await self._session.execute(
            text(
                "UPDATE exotel_inbound_events SET "
                "  intake_status = 'completed', "
                "  completed_at = NOW(), "
                "  claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL "
                "WHERE id = :eid AND business_id = :bid "
                "  AND claim_token = :token "
                "  AND claim_version = :version AND intake_status = 'processing' "
                "  AND lease_expires_at >= NOW()"
            ),
            {"eid": event_id, "bid": business_id, "token": claim_token, "version": claim_version},
        )
        await self._session.flush()
        return result.rowcount > 0  # type: ignore[union-attr]

    async def mark_failed(
        self, event_id: int, business_id: int, claim_token: str, claim_version: int
    ) -> bool:
        """Mark failed with attempt-indexed backoff. Dead-letter after max. Caller commits.

        Requires unexpired lease (same fencing as mark_completed).
        """
        result = await self._session.execute(
            text(
                "UPDATE exotel_inbound_events SET "
                "  intake_status = CASE "
                "    WHEN attempts >= max_attempts THEN 'dead_letter' "
                "    ELSE 'failed' END, "
                "  next_attempt_at = CASE "
                "    WHEN attempts >= max_attempts THEN NULL "
                "    ELSE NOW() + make_interval(secs => "
                "      (ARRAY[30,60,120,300,600])[LEAST(attempts, 5)]) END, "
                "  dead_lettered_at = CASE "
                "    WHEN attempts >= max_attempts THEN NOW() ELSE NULL END, "
                "  claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL "
                "WHERE id = :eid AND business_id = :bid "
                "  AND claim_token = :token "
                "  AND claim_version = :version AND intake_status = 'processing' "
                "  AND lease_expires_at >= NOW()"
            ),
            {"eid": event_id, "bid": business_id, "token": claim_token, "version": claim_version},
        )
        await self._session.flush()
        return result.rowcount > 0  # type: ignore[union-attr]
