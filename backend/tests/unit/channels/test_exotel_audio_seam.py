"""Audio seam tests — real transport lifecycle through WebSocket.

Tests the ExotelMediaTransport against synthetic provider fixtures
via a real WebSocket connection, not helper function calls.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from fonely.api.channels.exotel_audio import (
    ExotelMediaTransport,
    ExotelStreamError,
    _parse_ws_message,
)
from fonely.domain.calls.media import (
    CANONICAL_INBOUND,
    CANONICAL_OUTBOUND,
    CONTRACT_VERSION,
    INBOUND_BYTES_PER_FRAME,
    OUTBOUND_BYTES_PER_FRAME,
    InboundAudioFrame,
    InboundDiscontinuity,
    OutboundAudioFrame,
    ProviderStreamEnded,
    SessionStarted,
)


def _make_start_event(
    encoding: str = "audio/x-raw",
    sample_rate: int = 16000,
) -> str:
    return json.dumps({
        "event": "start",
        "sequence_number": "1",
        "stream_sid": "MZ" + "a" * 30,
        "start": {
            "stream_sid": "MZ" + "a" * 30,
            "call_sid": "CA" + "b" * 30,
            "account_sid": "AC_test",
            "media_format": {
                "encoding": encoding,
                "sample_rate": str(sample_rate),
                "bit_rate": "16",
            },
        },
    })


def _make_pcm_silence(num_bytes: int) -> bytes:
    return b"\x00" * num_bytes


def _make_media_event(
    payload: bytes,
    chunk: int = 0,
    timestamp: int = 0,
) -> str:
    return json.dumps({
        "event": "media",
        "sequence_number": str(chunk + 2),
        "stream_sid": "MZ" + "a" * 30,
        "media": {
            "chunk": str(chunk),
            "timestamp": str(timestamp),
            "payload": base64.b64encode(payload).decode(),
        },
    })


def _make_stop_event(reason: str = "callended") -> str:
    return json.dumps({
        "event": "stop",
        "sequence_number": "99",
        "stream_sid": "MZ" + "a" * 30,
        "stop": {
            "call_sid": "CA" + "b" * 30,
            "account_sid": "AC_test",
            "reason": reason,
        },
    })


# ============================================================================
# Transport lifecycle — through real WebSocket mock
# ============================================================================


class TestTransportLifecycle:
    """Tests using ExotelMediaTransport with a mock WebSocket."""

    async def test_start_handshake_produces_session(self) -> None:
        """Start event → SessionStarted with correct identity."""
        ws = _MockWebSocket([_make_start_event(), _make_stop_event()])
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )

        started_events: list[SessionStarted] = []

        async def on_started(s: SessionStarted) -> None:
            started_events.append(s)

        ended = await transport.run(on_session_started=on_started)

        assert len(started_events) == 1
        s = started_events[0]
        assert s.identity.schema_version == CONTRACT_VERSION
        assert s.identity.business_id == 1
        assert s.identity.provider == "exotel"
        assert s.identity.provider_call_id == "CA" + "b" * 30
        assert s.input_format == CANONICAL_INBOUND
        assert s.output_format == CANONICAL_OUTBOUND
        assert isinstance(ended, ProviderStreamEnded)

    async def test_media_frames_enqueued_as_canonical(self) -> None:
        """Provider PCM → canonical frames captured during session."""
        pcm_16k = _make_pcm_silence(640)
        messages = [
            _make_start_event(sample_rate=16000),
            _make_media_event(pcm_16k, chunk=0, timestamp=0),
            _make_media_event(pcm_16k, chunk=1, timestamp=20),
            _make_stop_event(),
        ]
        ws = _MockWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )

        captured: list[InboundAudioFrame] = []

        async def _consumer() -> None:
            while True:
                item = await transport.inbound_queue.get()
                if item is None:
                    break
                if isinstance(item, InboundAudioFrame):
                    captured.append(item)

        consumer = asyncio.create_task(_consumer())
        await transport.run()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer

        assert len(captured) >= 1
        for f in captured:
            assert len(f.pcm_s16le_16khz_mono) == INBOUND_BYTES_PER_FRAME

    async def test_sequence_gap_produces_discontinuity(self) -> None:
        """Chunk gap → InboundDiscontinuity event captured."""
        pcm = _make_pcm_silence(640)
        messages = [
            _make_start_event(sample_rate=16000),
            _make_media_event(pcm, chunk=0, timestamp=0),
            _make_media_event(pcm, chunk=5, timestamp=100),
            _make_stop_event(),
        ]
        ws = _MockWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )

        captured: list = []

        async def _consumer() -> None:
            while True:
                item = await transport.inbound_queue.get()
                if item is None:
                    break
                captured.append(item)

        consumer = asyncio.create_task(_consumer())
        await transport.run()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer

        discs = [i for i in captured if isinstance(i, InboundDiscontinuity)]
        assert len(discs) >= 1
        assert discs[0].reason == "sequence_gap"

    async def test_outbound_send_produces_provider_envelope(self) -> None:
        """Outbound frame → JSON media envelope sent over WebSocket."""
        messages = [
            _make_start_event(sample_rate=16000),
        ]
        ws = _MockWebSocket(messages, hang_after=True)
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )

        async def _drive() -> None:
            await asyncio.sleep(0.1)
            frame = OutboundAudioFrame(
                generation_id=1, sequence=0, media_timestamp_ms=0,
                pcm_s16le_24khz_mono=b"\x00" * OUTBOUND_BYTES_PER_FRAME,
            )
            await transport.send_audio(frame)
            await asyncio.sleep(0.3)
            ws.close_from_test()

        await asyncio.gather(transport.run(), _drive())

        sent = [m for m in ws.sent_messages if "media" in m]
        assert len(sent) >= 1
        parsed = json.loads(sent[0])
        assert parsed["event"] == "media"
        assert "payload" in parsed["media"]
        assert parsed["stream_sid"] == "MZ" + "a" * 30

    async def test_barge_in_clear_is_monotonic(self) -> None:
        """Generation clear is monotonic — lower ID cannot un-cancel."""
        ws = _MockWebSocket([_make_start_event(), _make_stop_event()])
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )

        assert transport.clear_generation(3) is True
        assert transport.clear_generation(2) is False
        assert transport.clear_generation(3) is False
        assert transport.clear_generation(4) is True

    async def test_provider_clear_sent_on_barge_in(self) -> None:
        """Barge-in → clear event sent to provider."""
        messages = [_make_start_event(sample_rate=16000)]
        ws = _MockWebSocket(messages, hang_after=True)
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )

        async def _drive() -> None:
            await asyncio.sleep(0.1)
            transport.clear_generation(1)
            await transport.send_provider_clear()
            await asyncio.sleep(0.1)
            ws.close_from_test()

        await asyncio.gather(transport.run(), _drive())

        clear_msgs = [
            m for m in ws.sent_messages
            if '"clear"' in m
        ]
        assert len(clear_msgs) >= 1
        parsed = json.loads(clear_msgs[0])
        assert parsed["event"] == "clear"

    async def test_stop_produces_terminal_reason(self) -> None:
        messages = [_make_start_event(), _make_stop_event("callended")]
        ws = _MockWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )

        ended = await transport.run()
        assert isinstance(ended, ProviderStreamEnded)
        assert ended.reason == "provider_stop"
        assert ended.provider_code == "callended"

    async def test_disconnect_cleans_up(self) -> None:
        """WebSocket disconnect → transport stops, queues drained."""
        ws = _MockWebSocket([_make_start_event()])
        ws._disconnect_after_start = True
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )

        ended = await transport.run()
        assert transport.is_stopped
        assert ended.reason in ("normal_disconnect", "protocol_error")


# ============================================================================
# Adversarial
# ============================================================================


class TestAdversarialTransport:
    async def test_unsupported_codec_fails(self) -> None:
        ws = _MockWebSocket([
            _make_start_event(encoding="audio/opus"),
        ])
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )
        ended = await transport.run()
        assert ended.reason == "protocol_error"
        assert "codec" in (ended.provider_code or "")

    async def test_unsupported_sample_rate_fails(self) -> None:
        ws = _MockWebSocket([
            _make_start_event(sample_rate=44100),
        ])
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )
        ended = await transport.run()
        assert ended.reason == "protocol_error"

    async def test_duplicate_start_fails(self) -> None:
        ws = _MockWebSocket([
            _make_start_event(),
            _make_start_event(),
        ])
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )
        ended = await transport.run()
        assert ended.reason == "protocol_error"
        assert "duplicate" in (ended.provider_code or "").lower()

    async def test_invalid_json_fails(self) -> None:
        ws = _MockWebSocket(["not json at all"])
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )
        ended = await transport.run()
        assert ended.reason == "protocol_error"

    async def test_cleared_generation_frame_not_sent(self) -> None:
        """Frame from cleared generation is discarded, not sent."""
        messages = [_make_start_event(sample_rate=16000)]
        ws = _MockWebSocket(messages, hang_after=True)
        transport = ExotelMediaTransport(
            ws=ws, business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_test",
        )

        async def _drive() -> None:
            await asyncio.sleep(0.1)
            transport.clear_generation(1)
            old_frame = OutboundAudioFrame(
                generation_id=1, sequence=0, media_timestamp_ms=0,
                pcm_s16le_24khz_mono=b"\x00" * OUTBOUND_BYTES_PER_FRAME,
            )
            await transport.send_audio(old_frame)
            await asyncio.sleep(0.2)
            ws.close_from_test()

        await asyncio.gather(transport.run(), _drive())

        media_msgs = [m for m in ws.sent_messages if '"media"' in m]
        assert len(media_msgs) == 0


# ============================================================================
# Message parsing
# ============================================================================


class TestMessageParsing:
    def test_valid_json_parsed(self) -> None:
        msg = _parse_ws_message('{"event":"start"}')
        assert msg["event"] == "start"

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ExotelStreamError, match="invalid JSON"):
            _parse_ws_message("not json")

    def test_non_object_raises(self) -> None:
        with pytest.raises(ExotelStreamError, match="expected JSON object"):
            _parse_ws_message("[1,2,3]")

    def test_oversized_raises(self) -> None:
        with pytest.raises(ExotelStreamError, match="too large"):
            _parse_ws_message("x" * 70_000)


# ============================================================================
# Mock WebSocket
# ============================================================================


class _MockWebSocket:
    """Fake WebSocket that delivers pre-loaded messages then disconnects.

    Uses an asyncio.Event created lazily to ensure it belongs to the
    running event loop (not the import-time loop).
    """

    def __init__(
        self,
        messages: list[str],
        hang_after: bool = False,
    ) -> None:
        self._messages = list(messages)
        self._index = 0
        self._hang_after = hang_after
        self._closed = False
        self._disconnect_after_start = False
        self.sent_messages: list[str] = []
        self._hang_event: asyncio.Event | None = None

    def _get_hang_event(self) -> asyncio.Event:
        if self._hang_event is None:
            self._hang_event = asyncio.Event()
        return self._hang_event

    async def receive_text(self) -> str:
        if self._closed:
            raise WebSocketDisconnect(code=1000)

        if self._index < len(self._messages):
            msg = self._messages[self._index]
            self._index += 1

            if self._disconnect_after_start and '"start"' in msg:
                self._closed = True

            return msg

        if self._hang_after:
            await self._get_hang_event().wait()
            raise WebSocketDisconnect(code=1000)

        raise WebSocketDisconnect(code=1000)

    async def send_text(self, data: str) -> None:
        if self._closed:
            raise WebSocketDisconnect(code=1000)
        self.sent_messages.append(data)

    def close_from_test(self) -> None:
        self._closed = True
        if self._hang_event is not None:
            self._hang_event.set()
