"""In-memory InboundCallEventIntake for contract testing.

Validates semantic idempotency, immutable payload, and forward-only
state transitions — the same invariants the production repository
must enforce.
"""

from __future__ import annotations

import hashlib
import json

from fonely.domain.calls.events import ExotelCallbackEvent
from fonely.domain.calls.intake import (
    DuplicateCallEventError,
    InboundCallEventRecord,
)
from fonely.domain.calls.transitions import (
    validate_transition,
)


class InMemoryCallEventIntake:
    """Test double that enforces the same contract as production."""

    def __init__(self) -> None:
        self._events: list[InboundCallEventRecord] = []
        self._next_id = 1
        # (business_id, call_sid, event_type) → record for semantic dedup
        self._seen: dict[tuple[int, str, str], InboundCallEventRecord] = {}
        # (business_id, call_sid) → latest status for transition validation
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

        # Validate forward-only transition
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
        return record

    @property
    def events(self) -> list[InboundCallEventRecord]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()
        self._seen.clear()
        self._call_status.clear()
        self._next_id = 1
        self.persist_called = False
        self.persist_count = 0
