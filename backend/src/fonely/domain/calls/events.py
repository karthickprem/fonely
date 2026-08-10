"""Typed Exotel callback event model and strict validation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ExotelEventType(StrEnum):
    ANSWERED = "answered"
    TERMINAL = "terminal"


class ExotelCallStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BUSY = "busy"
    NO_ANSWER = "no-answer"


_TERMINAL_STATUSES = frozenset(
    {
        ExotelCallStatus.COMPLETED,
        ExotelCallStatus.FAILED,
        ExotelCallStatus.BUSY,
        ExotelCallStatus.NO_ANSWER,
    }
)

_VALID_STATUSES = frozenset(ExotelCallStatus)
_VALID_EVENT_TYPES = frozenset(ExotelEventType)
_VALID_DIRECTIONS = frozenset({"inbound", "outbound-dial", "outbound-api"})


class ExotelCallbackParseError(ValueError):
    """Strict parse failure — adapter returns 400, no coercion."""


class ExotelCallbackEvent:
    """Canonical typed representation of an Exotel status callback.

    All field names match the Exotel API response documentation.
    Open question OQ-1: exact callback field names require sandbox
    verification.
    """

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


def _strict_nonneg_int(value: Any, field: str) -> int | None:
    """Parse a non-negative integer or None. Reject negatives and non-numeric."""
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (ValueError, TypeError) as exc:
        raise ExotelCallbackParseError(f"invalid {field}: {value!r}") from exc
    if parsed < 0:
        raise ExotelCallbackParseError(f"negative {field}: {parsed}")
    return parsed


def parse_exotel_callback(data: dict[str, Any]) -> ExotelCallbackEvent:
    """Parse a flat dict into a typed event with strict validation.

    Rejects (not coerces) invalid Duration, EventType, Direction, and
    identity fields. Every rejection raises ExotelCallbackParseError.
    """
    call_sid = str(data.get("CallSid") or "").strip()
    if not call_sid:
        raise ExotelCallbackParseError("missing CallSid")

    raw_status = str(data.get("Status") or "").strip().lower()
    if raw_status not in _VALID_STATUSES:
        raise ExotelCallbackParseError(f"unrecognized status: {raw_status!r}")

    event_type_raw = str(data.get("EventType") or "").strip().lower()
    if event_type_raw and event_type_raw not in _VALID_EVENT_TYPES:
        raise ExotelCallbackParseError(f"invalid EventType: {event_type_raw!r}")
    if not event_type_raw:
        event_type_raw = "terminal" if raw_status in _TERMINAL_STATUSES else "answered"

    caller_phone = str(data.get("From") or "").strip()
    called_number = str(data.get("To") or "").strip()
    if not caller_phone or not called_number:
        raise ExotelCallbackParseError("missing From or To")

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
        custom_field=str(data.get("CustomField") or "").strip() or None,
    )
