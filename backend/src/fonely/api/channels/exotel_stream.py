"""Exotel WebSocket stream entrypoint — admission, auth, Pipecat handoff.

Owns: gateway auth, tenant binding, start-event validation, sample rate
derivation, ExotelFrameSerializer configuration, lifecycle cleanup.
Does NOT own: codec conversion, frame serialization, VAD, STT, LLM, TTS.

Pipecat's ExotelFrameSerializer (pipecat.serializers.exotel, pinned 1.7)
handles all audio codec/resample/framing. This module configures it
from the provider's start event metadata.

Sample rate: derived from start.media_format.sample_rate (authoritative
transport metadata after gateway auth). Fail closed on absent/malformed/
unsupported rate. Never default to 8000 on this path.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fonely.api.channels.exotel_admission import (
    verify_gateway_secret,
)

logger = logging.getLogger("fonely.api.channels.exotel_stream")

_MAX_START_MESSAGE = 16_384
_SUPPORTED_RATES = frozenset({8000, 16000, 24000})
_SUPPORTED_CODECS = frozenset({"audio/x-raw"})
_HANDSHAKE_TIMEOUT_S = 10


class ExotelStartValidationError(Exception):
    """Start event validation failed — close with protocol error."""


def validate_start_event(msg: dict[str, Any]) -> tuple[str, str, int]:
    """Validate and extract identity + rate from Exotel start event.

    Returns (stream_sid, call_sid, sample_rate).
    Raises ExotelStartValidationError on any invalid/absent field.
    """
    start_data = msg.get("start")
    if not isinstance(start_data, dict):
        raise ExotelStartValidationError("missing or invalid start payload")

    stream_sid = start_data.get("stream_sid", "")
    call_sid = start_data.get("call_sid", "")
    if not stream_sid or not call_sid:
        raise ExotelStartValidationError("missing stream_sid or call_sid")

    media_format = start_data.get("media_format")
    if not isinstance(media_format, dict):
        raise ExotelStartValidationError("missing media_format in start")

    encoding = media_format.get("encoding", "")
    if encoding not in _SUPPORTED_CODECS:
        raise ExotelStartValidationError(
            f"unsupported codec: {encoding!r}"
        )

    raw_rate = media_format.get("sample_rate")
    if raw_rate is None:
        raise ExotelStartValidationError(
            "missing sample_rate in media_format"
        )

    try:
        sample_rate = int(raw_rate)
    except (ValueError, TypeError) as exc:
        raise ExotelStartValidationError(
            f"malformed sample_rate: {raw_rate!r}"
        ) from exc

    if sample_rate not in _SUPPORTED_RATES:
        raise ExotelStartValidationError(
            f"unsupported sample_rate: {sample_rate}"
        )

    channels = media_format.get("channels")
    if channels is not None and str(channels) != "1":
        raise ExotelStartValidationError(
            f"unsupported channels: {channels} (expected mono)"
        )

    return stream_sid, call_sid, sample_rate


def check_rate_drift(
    declared_rate: int,
    expected_rate: int,
) -> bool:
    """Check if declared rate differs from our configured expectation.

    Returns True if drift detected (rates differ but both supported).
    Caller should alert and mark session as provisioning_drift=true.
    """
    return declared_rate != expected_rate


def parse_ws_start_message(raw: str) -> dict[str, Any]:
    """Parse a WebSocket text message as a start event.

    Raises ExotelStartValidationError on malformed input.
    """
    if len(raw) > _MAX_START_MESSAGE:
        raise ExotelStartValidationError("start message too large")

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExotelStartValidationError("invalid JSON") from exc

    if not isinstance(msg, dict):
        raise ExotelStartValidationError("expected JSON object")

    return msg
