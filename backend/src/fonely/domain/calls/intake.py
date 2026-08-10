"""Typed generic inbound call event intake interface.

The adapter calls this to persist a validated, normalized, immutable
inbound event. Production implementation will use a dedicated
exotel_inbound_events table (migration blocker — after Dev3's 0015 and
integrated head is known). The adapter NEVER mutates domain state
(calls table, conversation, owner commands) directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fonely.domain.calls.events import ExotelCallbackEvent


class DuplicateCallEventError(Exception):
    """Semantic duplicate: same CallSid+EventType already persisted."""


@dataclass(frozen=True, slots=True)
class InboundCallEventRecord:
    """Immutable record persisted by the intake."""

    id: int
    business_id: int
    call_sid: str
    event_type: str
    status: str
    caller_phone: str
    called_number: str
    duration: int | None
    direction: str | None
    payload_digest: str


class InboundCallEventIntake(Protocol):
    """Interface for durable call event persistence.

    Implementations must:
    - Persist before returning (no background queue)
    - Enforce (business_id, call_sid, event_type) semantic idempotency
    - Raise DuplicateCallEventError on duplicate
    - Never mutate domain state (calls, conversations, etc.)
    """

    async def persist(
        self,
        business_id: int,
        event: ExotelCallbackEvent,
    ) -> InboundCallEventRecord: ...
