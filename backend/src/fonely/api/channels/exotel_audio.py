"""Exotel WebSocket audio transport — real provider edge.

A real WebSocket connection through the app: authenticated, admitted,
tenant/call bound, bidirectional audio through bounded queues with
barge-in, cleanup on disconnect.

Provider codec: raw PCM s16le (audio/x-raw), default 8kHz, request 16kHz.
Falls back to μ-law if Legs API path delivers audio/x-mulaw.
Fails closed on unknown codec — never guesses.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import contextlib
import json
import logging
import time
from typing import Any
from uuid import uuid4

from starlette.websockets import WebSocket, WebSocketDisconnect

from fonely.domain.calls.media import (
    BACKPRESSURE_DEADLINE_MS,
    CANONICAL_INBOUND,
    CANONICAL_OUTBOUND,
    CONTRACT_VERSION,
    INBOUND_BYTES_PER_FRAME,
    INBOUND_QUEUE_MAX_FRAMES,
    OUTBOUND_QUEUE_MAX_FRAMES,
    InboundAudioFrame,
    InboundDiscontinuity,
    MediaSessionIdentity,
    OutboundAudioFrame,
    ProviderStreamEnded,
    SessionStarted,
)

logger = logging.getLogger("fonely.api.channels.exotel_audio")

_MAX_WS_MESSAGE = 65_536
_HANDSHAKE_TIMEOUT_S = 10
_BACKPRESSURE_S = BACKPRESSURE_DEADLINE_MS / 1000.0


class ExotelStreamError(Exception):
    """Protocol or codec error — terminates the stream."""


class _ResamplerState:
    """Persistent resampler state across chunks."""

    def __init__(self, from_rate: int, to_rate: int) -> None:
        self.from_rate = from_rate
        self.to_rate = to_rate
        self._state: tuple[int, ...] | None = None

    def convert(self, pcm: bytes) -> bytes:
        if self.from_rate == self.to_rate:
            return pcm
        result, self._state = audioop.ratecv(
            pcm, 2, 1, self.from_rate, self.to_rate, self._state
        )
        return result


class ExotelMediaTransport:
    """Full WebSocket transport lifecycle for one Exotel media stream.

    Constructed per-connection. Owns:
    - Start handshake and codec validation
    - Inbound: decode → resample → accumulate → emit canonical frames
    - Outbound: receive canonical frames → resample → encode → send
    - Sequence/timestamp integrity
    - Bounded queues with backpressure
    - Barge-in clear (monotonic generation)
    - Disconnect and cleanup
    """

    def __init__(
        self,
        ws: WebSocket,
        business_id: int,
        provider_environment: str,
        provider_account_id: str,
    ) -> None:
        self._ws = ws
        self._business_id = business_id
        self._environment = provider_environment
        self._account_id = provider_account_id

        self._session_id = uuid4()
        self._provider_call_id: str | None = None
        self._provider_stream_id: str | None = None
        self._codec: str | None = None
        self._provider_rate: int = 8000
        self._started = False
        self._stopped = False

        self._inbound_queue: asyncio.Queue[
            InboundAudioFrame | InboundDiscontinuity | None
        ] = asyncio.Queue(maxsize=INBOUND_QUEUE_MAX_FRAMES)
        self._outbound_queue: asyncio.Queue[
            OutboundAudioFrame | None
        ] = asyncio.Queue(maxsize=OUTBOUND_QUEUE_MAX_FRAMES)

        self._inbound_seq = 0
        self._expected_chunk = 0
        self._last_media_ts_ms = -1
        self._pcm_accumulator = bytearray()
        self._inbound_resampler: _ResamplerState | None = None
        self._outbound_resampler: _ResamplerState | None = None

        self._cleared_generation = -1
        self._ended_reason: ProviderStreamEnded | None = None

    async def run(
        self,
        on_session_started: Any = None,
    ) -> ProviderStreamEnded:
        """Run the full transport lifecycle. Returns terminal reason.

        Caller should wrap this in try/finally for admission release.
        """
        try:
            started = await self._receive_start()
            if on_session_started is not None:
                await on_session_started(started)

            recv_task = asyncio.create_task(self._receive_loop())
            send_task = asyncio.create_task(self._send_loop())

            try:
                await asyncio.gather(recv_task, send_task)
            except ExotelStreamError as exc:
                logger.warning(
                    "stream_protocol_error",
                    extra={
                        "business_id": self._business_id,
                        "error": str(exc),
                    },
                )
                self._ended_reason = ProviderStreamEnded(
                    reason="protocol_error", provider_code=str(exc)
                )
            except WebSocketDisconnect:
                self._ended_reason = ProviderStreamEnded(
                    reason="normal_disconnect", provider_code=None
                )
            except asyncio.CancelledError:
                self._ended_reason = ProviderStreamEnded(
                    reason="shutdown", provider_code=None
                )
            finally:
                recv_task.cancel()
                send_task.cancel()
                await asyncio.gather(
                    recv_task, send_task, return_exceptions=True
                )

        except ExotelStreamError as exc:
            self._ended_reason = ProviderStreamEnded(
                reason="protocol_error", provider_code=str(exc)
            )
        except WebSocketDisconnect:
            self._ended_reason = ProviderStreamEnded(
                reason="normal_disconnect", provider_code=None
            )

        if self._ended_reason is None:
            self._ended_reason = ProviderStreamEnded(
                reason="normal_disconnect", provider_code=None
            )

        self._stopped = True
        await self._drain_queues()
        return self._ended_reason

    async def _receive_start(self) -> SessionStarted:
        """Wait for the start event with handshake timeout."""
        try:
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT_S):
                raw = await self._ws.receive_text()
        except TimeoutError as exc:
            raise ExotelStreamError("start handshake timeout") from exc

        msg = _parse_ws_message(raw)
        event = msg.get("event", "")

        if event == "connected":
            try:
                async with asyncio.timeout(_HANDSHAKE_TIMEOUT_S):
                    raw = await self._ws.receive_text()
            except TimeoutError as exc:
                raise ExotelStreamError(
                    "start event timeout after connected"
                ) from exc
            msg = _parse_ws_message(raw)
            event = msg.get("event", "")

        if event != "start":
            raise ExotelStreamError(
                f"expected start event, got {event!r}"
            )

        start_data = msg.get("start", {})
        stream_sid = start_data.get("stream_sid", "")
        call_sid = start_data.get("call_sid", "")
        if not stream_sid or not call_sid:
            raise ExotelStreamError("missing stream_sid or call_sid")

        media_format = start_data.get("media_format", {})
        encoding = media_format.get("encoding", "")
        sample_rate = int(media_format.get("sample_rate", 8000))

        if encoding == "audio/x-raw":
            self._codec = "pcm"
        elif encoding in ("audio/x-mulaw", "audio/pcmu"):
            self._codec = "ulaw"
        else:
            raise ExotelStreamError(
                f"unsupported codec: {encoding!r}"
            )

        if sample_rate not in (8000, 16000, 24000):
            raise ExotelStreamError(
                f"unsupported sample rate: {sample_rate}"
            )

        self._provider_rate = sample_rate
        self._provider_call_id = call_sid
        self._provider_stream_id = stream_sid
        self._started = True

        self._inbound_resampler = _ResamplerState(
            self._provider_rate, CANONICAL_INBOUND.sample_rate
        )
        self._outbound_resampler = _ResamplerState(
            CANONICAL_OUTBOUND.sample_rate, self._provider_rate
        )

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

    async def _receive_loop(self) -> None:
        """Receive provider messages, decode, enqueue canonical frames."""
        while not self._stopped:
            try:
                raw = await self._ws.receive_text()
            except WebSocketDisconnect:
                await self._signal_end()
                return

            msg = _parse_ws_message(raw)
            event = msg.get("event", "")

            if event == "media":
                await self._handle_media(msg)
            elif event == "stop":
                stop_data = msg.get("stop", {})
                reason = stop_data.get("reason", "")
                if "error" in reason.lower() or "network" in reason.lower():
                    self._ended_reason = ProviderStreamEnded(
                        reason="network_error"
                        if "network" in reason.lower()
                        else "protocol_error",
                        provider_code=reason,
                    )
                else:
                    self._ended_reason = ProviderStreamEnded(
                        reason="provider_stop", provider_code=reason
                    )
                await self._signal_end()
                return
            elif event == "mark" or event == "dtmf" or event == "connected":
                pass
            elif event == "start":
                raise ExotelStreamError("duplicate start event")
            else:
                raise ExotelStreamError(
                    f"unknown event type: {event!r}"
                )

    async def _handle_media(self, msg: dict[str, Any]) -> None:
        media = msg.get("media", {})
        payload_b64 = media.get("payload", "")
        if not payload_b64 or not isinstance(payload_b64, str):
            return

        try:
            raw_audio = base64.b64decode(payload_b64, validate=True)
        except Exception as exc:
            raise ExotelStreamError("invalid base64 payload") from exc

        if len(raw_audio) > _MAX_WS_MESSAGE:
            raise ExotelStreamError(
                f"audio payload too large: {len(raw_audio)}"
            )

        chunk = media.get("chunk")
        timestamp = media.get("timestamp")

        if chunk is not None:
            chunk_num = int(chunk)
            if chunk_num < self._expected_chunk:
                return
            if chunk_num > self._expected_chunk:
                disc = InboundDiscontinuity(
                    expected_sequence=self._expected_chunk,
                    received_sequence=chunk_num,
                    reason="sequence_gap",
                )
                self._expected_chunk = chunk_num + 1
                await self._enqueue_inbound(disc)
                # Still process the audio from this chunk
            else:
                self._expected_chunk = chunk_num + 1

        if timestamp is not None:
            ts_ms = int(timestamp)
            if ts_ms < self._last_media_ts_ms and self._last_media_ts_ms >= 0:
                disc = InboundDiscontinuity(
                    expected_sequence=self._inbound_seq,
                    received_sequence=self._inbound_seq,
                    reason="provider_reset",
                )
                await self._enqueue_inbound(disc)
                self._last_media_ts_ms = ts_ms
            else:
                self._last_media_ts_ms = ts_ms
        else:
            ts_ms = self._inbound_seq * CANONICAL_INBOUND.frame_duration_ms

        pcm_provider = audioop.ulaw2lin(raw_audio, 2) if self._codec == "ulaw" else raw_audio

        assert self._inbound_resampler is not None
        pcm_canonical = self._inbound_resampler.convert(pcm_provider)
        self._pcm_accumulator.extend(pcm_canonical)

        while len(self._pcm_accumulator) >= INBOUND_BYTES_PER_FRAME:
            frame_bytes = bytes(
                self._pcm_accumulator[:INBOUND_BYTES_PER_FRAME]
            )
            del self._pcm_accumulator[:INBOUND_BYTES_PER_FRAME]

            frame = InboundAudioFrame(
                sequence=self._inbound_seq,
                media_timestamp_ms=ts_ms,
                received_monotonic_ns=time.monotonic_ns(),
                pcm_s16le_16khz_mono=frame_bytes,
            )
            self._inbound_seq += 1
            await self._enqueue_inbound(frame)

    async def _enqueue_inbound(
        self, item: InboundAudioFrame | InboundDiscontinuity
    ) -> None:
        try:
            async with asyncio.timeout(_BACKPRESSURE_S):
                await self._inbound_queue.put(item)
        except TimeoutError:
            self._ended_reason = ProviderStreamEnded(
                reason="capacity_exceeded", provider_code="inbound_backpressure"
            )
            raise ExotelStreamError("inbound backpressure exceeded") from None

    async def _send_loop(self) -> None:
        """Dequeue outbound frames, encode, and send over WebSocket."""
        while True:
            try:
                frame = await asyncio.wait_for(
                    self._outbound_queue.get(), timeout=0.1
                )
            except TimeoutError:
                if self._stopped:
                    return
                continue

            if frame is None:
                return

            if frame.generation_id <= self._cleared_generation:
                continue

            assert self._outbound_resampler is not None
            if self._codec == "ulaw":
                pcm_provider = self._outbound_resampler.convert(
                    frame.pcm_s16le_24khz_mono
                )
                encoded = base64.b64encode(
                    audioop.lin2ulaw(pcm_provider, 2)
                ).decode()
            else:
                pcm_provider = self._outbound_resampler.convert(
                    frame.pcm_s16le_24khz_mono
                )
                encoded = base64.b64encode(pcm_provider).decode()

            envelope = json.dumps({
                "event": "media",
                "stream_sid": self._provider_stream_id,
                "media": {"payload": encoded},
            })

            try:
                await self._ws.send_text(envelope)
            except (WebSocketDisconnect, RuntimeError):
                await self._signal_end()
                return

    async def send_audio(self, frame: OutboundAudioFrame) -> bool:
        """Enqueue outbound audio. Returns False on backpressure timeout."""
        if self._stopped:
            return False
        if frame.generation_id <= self._cleared_generation:
            return True
        try:
            async with asyncio.timeout(_BACKPRESSURE_S):
                await self._outbound_queue.put(frame)
            return True
        except TimeoutError:
            self._ended_reason = ProviderStreamEnded(
                reason="capacity_exceeded",
                provider_code="outbound_backpressure",
            )
            return False

    def clear_generation(self, generation_id: int) -> bool:
        """Clear outbound audio for this and older generations.

        Returns True if this was a forward clear. Returns False (no-op)
        if generation_id is not newer than current — monotonic, never
        un-cancels.
        """
        if generation_id <= self._cleared_generation:
            return False
        self._cleared_generation = generation_id
        return True

    async def send_provider_clear(self) -> None:
        """Send Exotel clear event to flush provider-side playback buffer."""
        if self._stopped or self._provider_stream_id is None:
            return
        envelope = json.dumps({
            "event": "clear",
            "stream_sid": self._provider_stream_id,
        })
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await self._ws.send_text(envelope)

    async def send_provider_mark(self, name: str) -> None:
        """Send a mark event for playback completion tracking."""
        if self._stopped or self._provider_stream_id is None:
            return
        envelope = json.dumps({
            "event": "mark",
            "stream_sid": self._provider_stream_id,
            "mark": {"name": name},
        })
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await self._ws.send_text(envelope)

    async def _signal_end(self) -> None:
        self._stopped = True
        with contextlib.suppress(asyncio.QueueFull):
            self._inbound_queue.put_nowait(None)
        with contextlib.suppress(asyncio.QueueFull):
            self._outbound_queue.put_nowait(None)

    async def _drain_queues(self) -> None:
        while not self._inbound_queue.empty():
            try:
                self._inbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        while not self._outbound_queue.empty():
            try:
                self._outbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    @property
    def inbound_queue(
        self,
    ) -> asyncio.Queue[InboundAudioFrame | InboundDiscontinuity | None]:
        return self._inbound_queue

    @property
    def session_id(self):
        return self._session_id

    @property
    def provider_call_id(self) -> str | None:
        return self._provider_call_id

    @property
    def is_stopped(self) -> bool:
        return self._stopped


def _parse_ws_message(raw: str) -> dict[str, Any]:
    if len(raw) > _MAX_WS_MESSAGE:
        raise ExotelStreamError("message too large")
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExotelStreamError("invalid JSON") from exc
    if not isinstance(msg, dict):
        raise ExotelStreamError("expected JSON object")
    return msg
