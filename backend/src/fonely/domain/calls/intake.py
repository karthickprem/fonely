"""Typed generic inbound call event intake interface.

The adapter NEVER mutates domain state directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fonely.domain.calls.events import ExotelCallbackEvent


class DuplicateCallEventError(Exception):
    """Semantic duplicate: same (business_id, call_sid, event_type) already persisted."""


class ConflictingCallEventError(Exception):
    """Same dedup key but different payload digest — conflicting terminal."""


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
    conversation_duration: int | None
    direction: str | None
    custom_field: str | None
    payload_digest: str


@dataclass(frozen=True, slots=True)
class ClaimedCallEvent:
    """Typed claimed event for worker processing — not a positional dict."""

    id: int
    call_sid: str
    business_id: int
    event_type: str
    status: str
    caller_phone: str
    called_number: str
    duration: int | None
    direction: str | None
    claim_token: str
    claim_version: int


class InboundCallEventIntake(Protocol):
    """Interface for durable call event persistence.

    Implementations must:
    - Persist before returning (no background queue)
    - Enforce (business_id, call_sid, event_type) semantic idempotency
    - Raise DuplicateCallEventError on exact duplicate
    - Raise ConflictingCallEventError on same key but different digest
    - Never mutate domain state (calls, conversations, etc.)
    """

    async def persist(
        self,
        business_id: int,
        event: ExotelCallbackEvent,
    ) -> InboundCallEventRecord: ...
