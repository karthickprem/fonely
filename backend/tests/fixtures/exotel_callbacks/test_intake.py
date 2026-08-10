"""In-memory InboundCallEventIntake with claim/complete/fail lifecycle.

Enforces semantic idempotency, digest-based duplicate vs conflict
detection, lease ownership, and dead-letter. Late events are persisted
durably — forward-only transition is the worker's responsibility.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fonely.domain.calls.intake import (
    ClaimedCallEvent,
    ConflictingCallEventError,
    DuplicateCallEventError,
    InboundCallEvent,
    InboundCallEventRecord,
    canonical_event_digest,
)


@dataclass
class _EventState:
    record: InboundCallEventRecord
    claim_token: str
    claim_version: int
    intake_status: str
    attempts: int
    max_attempts: int


class InMemoryCallEventIntake:
    """Test double enforcing the same contract as production."""

    def __init__(self) -> None:
        self._events: list[InboundCallEventRecord] = []
        self._state: dict[int, _EventState] = {}
        self._next_id = 1
        self._seen: dict[tuple[int, str, str, str], InboundCallEventRecord] = {}
        self.persist_called = False
        self.persist_count = 0

    async def persist(
        self,
        business_id: int,
        event: InboundCallEvent,
    ) -> InboundCallEventRecord:
        self.persist_called = True
        self.persist_count += 1

        digest = canonical_event_digest(event)
        dedup_key = (business_id, event.provider, event.provider_call_id, event.event_type)

        existing = self._seen.get(dedup_key)
        if existing is not None:
            if existing.payload_digest == digest:
                raise DuplicateCallEventError(
                    f"exact duplicate: {event.provider_call_id}/{event.event_type}"
                )
            raise ConflictingCallEventError(
                f"conflicting: {event.provider_call_id}/{event.event_type} "
                f"digest {digest} != {existing.payload_digest}"
            )

        record = InboundCallEventRecord(
            id=self._next_id,
            business_id=business_id,
            provider=event.provider,
            provider_call_id=event.provider_call_id,
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
        self._next_id += 1
        self._events.append(record)
        self._seen[dedup_key] = record
        self._state[record.id] = _EventState(
            record=record,
            claim_token="",
            claim_version=1,
            intake_status="received",
            attempts=0,
            max_attempts=5,
        )
        return record

    async def claim_next_eligible(self) -> ClaimedCallEvent | None:
        for eid, es in self._state.items():
            if es.intake_status in ("received", "failed"):
                token = str(uuid.uuid4())
                es.claim_token = token
                es.claim_version += 1
                es.intake_status = "processing"
                es.attempts += 1
                return ClaimedCallEvent(
                    id=eid,
                    provider=es.record.provider,
                    provider_call_id=es.record.provider_call_id,
                    business_id=es.record.business_id,
                    event_type=es.record.event_type,
                    status=es.record.status,
                    caller_phone=es.record.caller_phone,
                    called_number=es.record.called_number,
                    duration=es.record.duration,
                    direction=es.record.direction,
                    claim_token=token,
                    claim_version=es.claim_version,
                )
        return None

    async def mark_completed(
        self, event_id: int, business_id: int, claim_token: str, claim_version: int
    ) -> bool:
        es = self._state.get(event_id)
        if es is None or es.claim_token != claim_token or es.claim_version != claim_version:
            return False
        if es.record.business_id != business_id:
            return False
        if es.intake_status != "processing":
            return False
        es.intake_status = "completed"
        es.claim_token = ""
        return True

    async def mark_failed(
        self, event_id: int, business_id: int, claim_token: str, claim_version: int
    ) -> bool:
        es = self._state.get(event_id)
        if es is None or es.claim_token != claim_token or es.claim_version != claim_version:
            return False
        if es.record.business_id != business_id:
            return False
        if es.intake_status != "processing":
            return False
        if es.attempts >= es.max_attempts:
            es.intake_status = "dead_letter"
        else:
            es.intake_status = "failed"
        es.claim_token = ""
        return True

    @property
    def events(self) -> list[InboundCallEventRecord]:
        return list(self._events)

    def get_intake_status(self, event_id: int) -> str | None:
        es = self._state.get(event_id)
        return es.intake_status if es else None

    def get_attempts(self, event_id: int) -> int:
        es = self._state.get(event_id)
        return es.attempts if es else 0

    def clear(self) -> None:
        self._events.clear()
        self._seen.clear()
        self._state.clear()
        self._next_id = 1
        self.persist_called = False
        self.persist_count = 0
