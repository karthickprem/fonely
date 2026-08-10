"""Exotel WebSocket audio adapter — provider edge of the audio seam.

Owns: Exotel wire protocol parsing, G.711 μ-law decode/encode,
8kHz↔16kHz/24kHz resample, provider frame accumulation, sequence
tracking, backpressure, and disconnect.

Does NOT own: VAD, STT, LLM, TTS, runtime lifecycle, business logic.

Provider codec assumption: PCMU (G.711 μ-law) 8kHz mono until
sandbox verification (OQ). Fails on conflicting metadata.
"""

from __future__ import annotations

import audioop
import base64
import json
import logging
import time
from typing import Any
from uuid import UUID, uuid4

from fonely.domain.calls.media import (
    CANONICAL_INBOUND,
    CANONICAL_OUTBOUND,
    CONTRACT_VERSION,
    INBOUND_BYTES_PER_FRAME,
    OUTBOUND_BYTES_PER_FRAME,
    InboundAudioFrame,
    InboundDiscontinuity,
    MediaSessionIdentity,
    OutboundAudioFrame,
    ProviderStreamEnded,
    SessionStarted,
)

logger = logging.getLogger("fonely.api.channels.exotel_audio")

_MAX_WS_MESSAGE = 65_536
_MAX_CONTROL_MESSAGE = 16_384

EXPECTED_PROVIDER_CODEC = "audio/pcmu"
EXPECTED_PROVIDER_RATE = 8000


class ExotelStreamError(Exception):
    """Protocol or codec error on the Exotel WebSocket stream."""


class ExotelAudioAdapter:
    """Stateful per-stream adapter: Exotel wire ↔ canonical media events.

    Lifecycle:
      1. handle_start(start_message) → SessionStarted
      2. handle_media(media_message) → InboundAudioFrame | InboundDiscontinuity | None
      3. encode_outbound(OutboundAudioFrame) → provider wire bytes
      4. handle_stop(stop_message) → ProviderStreamEnded
    """

    def __init__(
        self,
        business_id: int,
        provider_environment: str,
        provider_account_id: str,
    ) -> None:
        self._business_id = business_id
        self._environment = provider_environment
        self._account_id = provider_account_id

        self._session_id: UUID | None = None
        self._provider_call_id: str | None = None
        self._provider_stream_id: str | None = None
        self._started = False

        self._inbound_seq = 0
        self._last_media_ts_ms = -1
        self._pcm_accumulator = bytearray()

        self._current_generation = -1

    def handle_start(self, msg: dict[str, Any]) -> SessionStarted:
        """Parse Exotel stream start message. Validates codec metadata."""
        if self._started:
            raise ExotelStreamError("duplicate start event")

        stream_sid = msg.get("streamSid", "")
        call_sid = msg.get("callSid", "")
        if not stream_sid or not call_sid:
            raise ExotelStreamError("missing streamSid or callSid")

        media_format = msg.get("mediaFormat", {})
        encoding = media_format.get("encoding", "")
        sample_rate = media_format.get("sampleRate")

        if encoding.lower() != "audio/pcmu":
            raise ExotelStreamError(
                f"unsupported codec: {encoding!r} (expected audio/pcmu)"
            )
        if sample_rate is not None and int(sample_rate) != EXPECTED_PROVIDER_RATE:
            raise ExotelStreamError(
                f"unsupported sample rate: {sample_rate} (expected {EXPECTED_PROVIDER_RATE})"
            )

        self._session_id = uuid4()
        self._provider_call_id = call_sid
        self._provider_stream_id = stream_sid
        self._started = True

        identity = MediaSessionIdentity(
            schema_version=CONTRACT_VERSION,
            session_id=self._session_id,
            business_id=self._business_id,
            provider="exotel",
            provider_environment=self._environment,
            provider_account_id=self._account_id,
            provider_call_id=call_sid,
            provider_stream_id=stream_sid,
        )

        return SessionStarted(
            identity=identity,
            input_format=CANONICAL_INBOUND,
            output_format=CANONICAL_OUTBOUND,
            started_monotonic_ns=time.monotonic_ns(),
        )

    def handle_media(
        self, msg: dict[str, Any]
    ) -> InboundAudioFrame | InboundDiscontinuity | None:
        """Parse media message, decode G.711 μ-law, resample 8k→16k, packetize."""
        if not self._started:
            raise ExotelStreamError("media before start")

        payload_b64 = msg.get("media", {}).get("payload", "")
        if not payload_b64 or not isinstance(payload_b64, str):
            return None

        try:
            raw_ulaw = base64.b64decode(payload_b64, validate=True)
        except Exception as exc:
            raise ExotelStreamError("invalid base64 audio payload") from exc

        if len(raw_ulaw) > _MAX_WS_MESSAGE:
            raise ExotelStreamError(
                f"audio payload too large: {len(raw_ulaw)}"
            )

        chunk_seq = msg.get("media", {}).get("chunk")
        media_ts = msg.get("media", {}).get("timestamp")

        if chunk_seq is not None:
            chunk_seq = int(chunk_seq)

        if media_ts is not None:
            media_ts_ms = int(media_ts)
            if media_ts_ms < self._last_media_ts_ms:
                return InboundDiscontinuity(
                    expected_sequence=self._inbound_seq,
                    received_sequence=self._inbound_seq,
                    reason="provider_reset",
                )
            self._last_media_ts_ms = media_ts_ms
        else:
            media_ts_ms = self._inbound_seq * 20

        pcm_8k = audioop.ulaw2lin(raw_ulaw, 2)
        pcm_16k, _state = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)

        self._pcm_accumulator.extend(pcm_16k)

        if len(self._pcm_accumulator) < INBOUND_BYTES_PER_FRAME:
            return None

        frame_bytes = bytes(self._pcm_accumulator[:INBOUND_BYTES_PER_FRAME])
        del self._pcm_accumulator[:INBOUND_BYTES_PER_FRAME]

        seq = self._inbound_seq
        self._inbound_seq += 1

        return InboundAudioFrame(
            sequence=seq,
            media_timestamp_ms=media_ts_ms,
            received_monotonic_ns=time.monotonic_ns(),
            pcm_s16le_16khz_mono=frame_bytes,
        )

    def encode_outbound(self, frame: OutboundAudioFrame) -> bytes:
        """Encode canonical 24kHz s16le → 8kHz G.711 μ-law for provider wire."""
        if len(frame.pcm_s16le_24khz_mono) != OUTBOUND_BYTES_PER_FRAME:
            raise ExotelStreamError(
                f"outbound frame wrong size: {len(frame.pcm_s16le_24khz_mono)}"
            )

        pcm_8k, _state = audioop.ratecv(
            frame.pcm_s16le_24khz_mono, 2, 1, 24000, 8000, None
        )
        ulaw = audioop.lin2ulaw(pcm_8k, 2)
        return base64.b64encode(ulaw)

    def handle_stop(
        self, msg: dict[str, Any] | None = None
    ) -> ProviderStreamEnded:
        """Parse stop message or generate disconnect event."""
        reason = "normal_disconnect"
        provider_code = None

        if msg is not None:
            stop_reason = msg.get("stop", {}).get("reason", "")
            if stop_reason:
                provider_code = stop_reason
                lower = stop_reason.lower()
                if "network" in lower:
                    reason = "network_error"
                elif "error" in lower:
                    reason = "protocol_error"
                else:
                    reason = "provider_stop"

        return ProviderStreamEnded(reason=reason, provider_code=provider_code)

    def handle_clear(self, generation_id: int) -> None:
        """Handle barge-in clear from runtime."""
        self._current_generation = generation_id

    def should_send(self, frame: OutboundAudioFrame) -> bool:
        """Check if outbound frame's generation is still active."""
        return frame.generation_id > self._current_generation

    @property
    def session_id(self) -> UUID | None:
        return self._session_id

    @property
    def provider_call_id(self) -> str | None:
        return self._provider_call_id


def parse_exotel_ws_message(raw: bytes | str) -> dict[str, Any]:
    """Parse and validate a raw WebSocket message from Exotel."""
    if isinstance(raw, bytes):
        if len(raw) > _MAX_WS_MESSAGE:
            raise ExotelStreamError(
                f"message too large: {len(raw)} bytes"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExotelStreamError("invalid UTF-8") from exc
    else:
        text = raw
        if len(text.encode("utf-8")) > _MAX_WS_MESSAGE:
            raise ExotelStreamError("message too large")

    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExotelStreamError("invalid JSON") from exc

    if not isinstance(msg, dict):
        raise ExotelStreamError("expected JSON object")

    event_type = msg.get("event", "")
    if not event_type or not isinstance(event_type, str):
        raise ExotelStreamError("missing event type")

    return msg
