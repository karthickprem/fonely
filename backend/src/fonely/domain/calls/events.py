"""Exotel-specific callback event model and strict validation.

This is the adapter-side DTO. The provider-neutral InboundCallEvent
lives in fonely.domain.calls.intake — adapters map into it before
calling the intake protocol.
"""

from __future__ import annotations

import re
from typing import Any

from fonely.domain.calls.intake import InboundCallEvent

EXOTEL_PROVIDER = "exotel"

_EXOTEL_STATUS_TO_CANONICAL = {
    "queued": "queued",
    "ringing": "ringing",
    "in-progress": "in_progress",
    "completed": "completed",
    "failed": "failed",
    "busy": "busy",
    "no-answer": "no_answer",
}

_TERMINAL_EXOTEL = frozenset({"completed", "failed", "busy", "no-answer"})
_ANSWERED_EXOTEL = frozenset({"in-progress"})
_VALID_EXOTEL_STATUSES = frozenset(_EXOTEL_STATUS_TO_CANONICAL.keys())
_VALID_EVENT_TYPES = frozenset({"answered", "terminal"})
_VALID_DIRECTIONS = frozenset({"inbound", "outbound-dial", "outbound-api"})

_CALL_SID_RE = re.compile(r"^[a-zA-Z0-9]{16,128}$")
_MAX_PHONE_LEN = 20
_MAX_CUSTOM_FIELD_LEN = 200
_MAX_DURATION_SECONDS = 86400


class ExotelCallbackParseError(ValueError):
    """Strict parse failure — adapter returns 400, no coercion."""


class ExotelCallbackEvent:
    """Exotel-specific typed callback. Adapter maps to InboundCallEvent."""

    __slots__ = (
        "call_sid",
        "called_number",
        "caller_phone",
        "conversation_duration",
        "custom_field",
        "direction",
        "duration",
        "event_type",
        "status",
    )

    def __init__(
        self,
        *,
        call_sid: str,
        event_type: str,
        status: str,
        caller_phone: str,
        called_number: str,
        duration: int | None = None,
        conversation_duration: int | None = None,
        direction: str | None = None,
        custom_field: str | None = None,
    ) -> None:
        self.call_sid = call_sid
        self.event_type = event_type
        self.status = status
        self.caller_phone = caller_phone
        self.called_number = called_number
        self.duration = duration
        self.conversation_duration = conversation_duration
        self.direction = direction
        self.custom_field = custom_field

    def to_inbound_event(self) -> InboundCallEvent:
        """Map Exotel DTO to provider-neutral InboundCallEvent.

        Normalizes Exotel hyphens to canonical underscores.
        Infers canonical event_type for queued (not answered).
        """
        canonical_status = _EXOTEL_STATUS_TO_CANONICAL[self.status]

        if self.event_type == "answered":
            canonical_event_type = "answered"
        elif self.event_type == "terminal":
            canonical_event_type = "terminal"
        elif self.status == "queued":
            canonical_event_type = "queued"
        elif self.status == "ringing":
            canonical_event_type = "ringing"
        else:
            canonical_event_type = self.event_type

        return InboundCallEvent(
            provider=EXOTEL_PROVIDER,
            provider_call_id=self.call_sid,
            event_type=canonical_event_type,
            status=canonical_status,
            caller_phone=self.caller_phone,
            called_number=self.called_number,
            conversation_duration=self.conversation_duration,
            custom_field=self.custom_field,
            direction=self.direction,
            duration=self.duration,
        )


def _strict_nonneg_int(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ExotelCallbackParseError(f"invalid {field}: boolean not accepted")
    if isinstance(value, float):
        raise ExotelCallbackParseError(f"invalid {field}: float not accepted")
    try:
        parsed = int(value)
    except (ValueError, TypeError) as exc:
        raise ExotelCallbackParseError(f"invalid {field}: {value!r}") from exc
    if parsed < 0:
        raise ExotelCallbackParseError(f"negative {field}: {parsed}")
    if parsed > _MAX_DURATION_SECONDS:
        raise ExotelCallbackParseError(
            f"{field} exceeds maximum ({_MAX_DURATION_SECONDS}s): {parsed}"
        )
    return parsed


def _validate_event_type_status_consistency(event_type: str, status: str) -> None:
    if event_type == "terminal" and status not in _TERMINAL_EXOTEL:
        raise ExotelCallbackParseError(
            f"EventType 'terminal' inconsistent with non-terminal status '{status}'"
        )
    if event_type == "answered" and status in _TERMINAL_EXOTEL:
        raise ExotelCallbackParseError(
            f"EventType 'answered' inconsistent with terminal status '{status}'"
        )


def _bounded_custom_field(raw: Any) -> str | None:
    val = str(raw or "").strip() or None
    if val is not None and len(val) > _MAX_CUSTOM_FIELD_LEN:
        raise ExotelCallbackParseError(f"CustomField exceeds {_MAX_CUSTOM_FIELD_LEN} chars")
    return val


def parse_exotel_callback(data: dict[str, Any]) -> ExotelCallbackEvent:
    """Parse a flat dict into a typed Exotel event with strict validation."""
    raw_sid = data.get("CallSid")
    if raw_sid is not None and not isinstance(raw_sid, str):
        raise ExotelCallbackParseError(f"CallSid must be string, got {type(raw_sid).__name__}")
    call_sid = (raw_sid or "").strip()
    if not call_sid:
        raise ExotelCallbackParseError("missing CallSid")
    if not _CALL_SID_RE.match(call_sid):
        raise ExotelCallbackParseError(f"invalid CallSid format: {call_sid!r}")

    raw_status = str(data.get("Status") or "").strip().lower()
    if raw_status not in _VALID_EXOTEL_STATUSES:
        raise ExotelCallbackParseError(f"unrecognized status: {raw_status!r}")

    event_type_raw = str(data.get("EventType") or "").strip().lower()
    if event_type_raw and event_type_raw not in _VALID_EVENT_TYPES:
        raise ExotelCallbackParseError(f"invalid EventType: {event_type_raw!r}")
    if not event_type_raw:
        if raw_status in _TERMINAL_EXOTEL:
            event_type_raw = "terminal"
        elif raw_status == "queued":
            event_type_raw = "queued"
        elif raw_status == "ringing":
            event_type_raw = "ringing"
        else:
            event_type_raw = "answered"

    if event_type_raw in _VALID_EVENT_TYPES:
        _validate_event_type_status_consistency(event_type_raw, raw_status)

    caller_phone = str(data.get("From") or "").strip()
    called_number = str(data.get("To") or "").strip()
    if not caller_phone or not called_number:
        raise ExotelCallbackParseError("missing From or To")
    if len(caller_phone) > _MAX_PHONE_LEN:
        raise ExotelCallbackParseError(f"From exceeds {_MAX_PHONE_LEN} chars")
    if len(called_number) > _MAX_PHONE_LEN:
        raise ExotelCallbackParseError(f"To exceeds {_MAX_PHONE_LEN} chars")

    direction_raw = str(data.get("Direction") or "").strip().lower() or None
    if direction_raw is not None and direction_raw not in _VALID_DIRECTIONS:
        raise ExotelCallbackParseError(f"invalid Direction: {direction_raw!r}")

    duration = _strict_nonneg_int(data.get("Duration"), "Duration")
    conv_dur = _strict_nonneg_int(data.get("ConversationDuration"), "ConversationDuration")
    if duration is not None and conv_dur is not None and conv_dur > duration:
        raise ExotelCallbackParseError(f"ConversationDuration ({conv_dur}) > Duration ({duration})")

    return ExotelCallbackEvent(
        call_sid=call_sid,
        event_type=event_type_raw,
        status=raw_status,
        caller_phone=caller_phone,
        called_number=called_number,
        duration=duration,
        conversation_duration=conv_dur,
        direction=direction_raw,
        custom_field=_bounded_custom_field(data.get("CustomField")),
    )
