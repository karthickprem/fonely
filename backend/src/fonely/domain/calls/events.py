"""Typed Exotel callback event model and validation."""

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


class ExotelCallbackParseError(ValueError):
    pass


class ExotelCallbackEvent:
    """Canonical typed representation of an Exotel status callback.

    Constructed from either JSON or multipart/form-data payloads after
    normalization to a flat dict. All field names match the Exotel API
    response documentation (CallSid, Status, From, To, etc.).

    Open question OQ-1: exact callback field names require sandbox
    verification. This model uses the documented API response field
    names as the best available evidence.
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


def _safe_nonneg_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed >= 0 else None


def parse_exotel_callback(data: dict[str, Any]) -> ExotelCallbackEvent:
    """Parse a flat dict (from JSON body or multipart fields) into a typed event.

    Raises ExotelCallbackParseError for missing required fields or
    unrecognized status values.
    """
    call_sid = str(data.get("CallSid") or "").strip()
    if not call_sid:
        raise ExotelCallbackParseError("missing CallSid")

    raw_status = str(data.get("Status") or "").strip().lower()
    if raw_status not in _VALID_STATUSES:
        raise ExotelCallbackParseError(f"unrecognized status: {raw_status!r}")

    event_type_raw = str(data.get("EventType") or "").strip().lower()
    if event_type_raw not in ("answered", "terminal"):
        event_type_raw = "terminal" if raw_status in _TERMINAL_STATUSES else "answered"

    caller_phone = str(data.get("From") or "").strip()
    called_number = str(data.get("To") or "").strip()
    if not caller_phone or not called_number:
        raise ExotelCallbackParseError("missing From or To")

    return ExotelCallbackEvent(
        call_sid=call_sid,
        event_type=event_type_raw,
        status=raw_status,
        caller_phone=caller_phone,
        called_number=called_number,
        duration=_safe_nonneg_int(data.get("Duration")),
        conversation_duration=_safe_nonneg_int(data.get("ConversationDuration")),
        direction=str(data.get("Direction") or "").strip() or None,
        custom_field=str(data.get("CustomField") or "").strip() or None,
    )
