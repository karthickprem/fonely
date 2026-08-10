"""In-memory InboundCallEventIntake with claim/complete/fail for worker tests.

Validates semantic idempotency, immutable payload, forward-only state
transitions, lease ownership, and dead-letter lifecycle — the same
invariants the production repository must enforce.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from fonely.domain.calls.events import ExotelCallbackEvent
from fonely.domain.calls.intake import (
    DuplicateCallEventError,
    InboundCallEventRecord,
)
from fonely.domain.calls.transitions import (
    validate_transition,
)


@dataclass
class ClaimedEvent:
    record: InboundCallEventRecord
    claim_token: str
    claim_version: int
    intake_status: str
    attempts: int
    max_attempts: int


class InMemoryCallEventIntake:
    """Test double that enforces the same contract as production."""

    def __init__(self) -> None:
        self._events: list[InboundCallEventRecord] = []
        self._claimed: dict[int, ClaimedEvent] = {}
        self._next_id = 1
        self._seen: dict[tuple[int, str, str], InboundCallEventRecord] = {}
        self._call_status: dict[tuple[int, str], str] = {}
        self.persist_called = False
        self.persist_count = 0

    async def persist(
        self,
        business_id: int,
        event: ExotelCallbackEvent,
    ) -> InboundCallEventRecord:
        self.persist_called = True
        self.persist_count += 1

        dedup_key = (business_id, event.call_sid, event.event_type)
        if dedup_key in self._seen:
            raise DuplicateCallEventError(f"duplicate: {event.call_sid}/{event.event_type}")

        call_key = (business_id, event.call_sid)
        current_status = self._call_status.get(call_key)
        new_status = validate_transition(current_status, event.status)
        self._call_status[call_key] = new_status

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
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]

        record = InboundCallEventRecord(
            id=self._next_id,
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
        self._next_id += 1
        self._events.append(record)
        self._seen[dedup_key] = record
        self._claimed[record.id] = ClaimedEvent(
            record=record,
            claim_token="",
            claim_version=1,
            intake_status="received",
            attempts=0,
            max_attempts=5,
        )
        return record

    async def claim_next_eligible(self) -> dict | None:
        """Claim one received/failed event. Returns None if queue empty."""
        for eid, ce in self._claimed.items():
            if ce.intake_status in ("received", "failed"):
                token = str(uuid.uuid4())
                ce.claim_token = token
                ce.claim_version += 1
                ce.intake_status = "processing"
                ce.attempts += 1
                return {
                    "id": eid,
                    "call_sid": ce.record.call_sid,
                    "business_id": ce.record.business_id,
                    "event_type": ce.record.event_type,
                    "status": ce.record.status,
                    "caller_phone": ce.record.caller_phone,
                    "called_number": ce.record.called_number,
                    "duration": ce.record.duration,
                    "direction": ce.record.direction,
                    "claim_token": token,
                    "claim_version": ce.claim_version,
                }
        return None

    async def mark_completed(self, event_id: int, claim_token: str, claim_version: int) -> bool:
        ce = self._claimed.get(event_id)
        if ce is None or ce.claim_token != claim_token or ce.claim_version != claim_version:
            return False
        if ce.intake_status != "processing":
            return False
        ce.intake_status = "completed"
        ce.claim_token = ""
        return True

    async def mark_failed(self, event_id: int, claim_token: str, claim_version: int) -> bool:
        ce = self._claimed.get(event_id)
        if ce is None or ce.claim_token != claim_token or ce.claim_version != claim_version:
            return False
        if ce.intake_status != "processing":
            return False
        if ce.attempts >= ce.max_attempts:
            ce.intake_status = "dead_letter"
        else:
            ce.intake_status = "failed"
        ce.claim_token = ""
        return True

    @property
    def events(self) -> list[InboundCallEventRecord]:
        return list(self._events)

    def get_intake_status(self, event_id: int) -> str | None:
        ce = self._claimed.get(event_id)
        return ce.intake_status if ce else None

    def get_attempts(self, event_id: int) -> int:
        ce = self._claimed.get(event_id)
        return ce.attempts if ce else 0

    def clear(self) -> None:
        self._events.clear()
        self._seen.clear()
        self._call_status.clear()
        self._claimed.clear()
        self._next_id = 1
        self.persist_called = False
        self.persist_count = 0
