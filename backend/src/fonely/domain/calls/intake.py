"""Provider-neutral inbound call event intake interface.

The adapter maps provider-specific DTOs (e.g. ExotelCallbackEvent) into
the neutral InboundCallEvent before calling persist. The intake protocol,
repository, and worker never reference provider-specific types.

The adapter NEVER mutates domain state directly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol


class DuplicateCallEventError(Exception):
    """Semantic duplicate: same dedup key already persisted with same digest."""


class ConflictingCallEventError(Exception):
    """Same dedup key but different payload digest."""


@dataclass(frozen=True, slots=True)
class InboundCallEvent:
    """Provider-neutral inbound call event.

    Adapters map their provider-specific DTOs into this before calling
    the intake. Fields are provider-neutral: no Exotel-specific naming.
    """

    provider: str
    provider_call_id: str
    event_type: str
    status: str
    caller_phone: str
    called_number: str
    conversation_duration: int | None
    custom_field: str | None
    direction: str | None
    duration: int | None


def canonical_event_digest(event: InboundCallEvent) -> str:
    """Canonical SHA-256 digest of all immutable event fields."""
    payload = json.dumps(
        {
            "called_number": event.called_number,
            "caller_phone": event.caller_phone,
            "conversation_duration": event.conversation_duration,
            "custom_field": event.custom_field,
            "direction": event.direction,
            "duration": event.duration,
            "event_type": event.event_type,
            "provider": event.provider,
            "provider_call_id": event.provider_call_id,
            "status": event.status,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class InboundCallEventRecord:
    """Immutable record persisted by the intake."""

    id: int
    business_id: int
    provider: str
    provider_call_id: str
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
    """Typed claimed event for worker processing."""

    id: int
    provider_call_id: str
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
    - Enforce (business_id, provider, provider_call_id, event_type) dedup
    - Raise DuplicateCallEventError on exact duplicate
    - Raise ConflictingCallEventError on same key but different digest
    - Persist ALL valid events including late lower-state
    - Never mutate domain state
    """

    async def persist(
        self,
        business_id: int,
        event: InboundCallEvent,
    ) -> InboundCallEventRecord: ...
