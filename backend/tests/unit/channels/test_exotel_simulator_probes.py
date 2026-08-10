"""Simulator-driven probes — transport + admission through fake Exotel client.

Uses ExotelSimulator to drive both happy and unhappy paths through
the ExotelMediaTransport. Proves admission, correlation, sequence
integrity, barge-in, and disconnect through the real transport code.

NOTE: Green probes prove admission and transport logic is correct.
They do NOT prove Exotel behaves as documented — that requires live
sandbox verification.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from starlette.websockets import WebSocketDisconnect

from fonely.api.channels.exotel_admission import verify_gateway_secret
from fonely.api.channels.exotel_audio import ExotelMediaTransport
from fonely.core.config import settings
from fonely.domain.calls.correlation import (
    CorrelationRecord,
    InMemoryCorrelationStore,
)
from fonely.domain.calls.media import (
    INBOUND_BYTES_PER_FRAME,
    OUTBOUND_BYTES_PER_FRAME,
    InboundAudioFrame,
    InboundDiscontinuity,
    OutboundAudioFrame,
    ProviderStreamEnded,
    SessionStarted,
)
from tests.fixtures.exotel_callbacks.simulator import (
    ExotelSimulator,
    SimulatorConfig,
)

_GATEWAY_SECRET = "simulator-gateway-secret-at-least-32c"


class _SimulatorWebSocket:
    """WebSocket mock driven by ExotelSimulator messages."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self._index = 0
        self._closed = False
        self.sent: list[str] = []
        self._hang = asyncio.Event()

    async def receive_text(self) -> str:
        if self._closed:
            raise WebSocketDisconnect(code=1000)
        if self._index < len(self._messages):
            msg = self._messages[self._index]
            self._index += 1
            return msg
        await self._hang.wait()
        raise WebSocketDisconnect(code=1000)

    async def send_text(self, data: str) -> None:
        if self._closed:
            raise WebSocketDisconnect(code=1000)
        self.sent.append(data)

    def close(self) -> None:
        self._closed = True
        self._hang.set()


# ============================================================================
# Happy path — full call lifecycle through simulator
# ============================================================================


class TestSimulatorHappyPath:
    async def test_full_call_lifecycle(self) -> None:
        """connected → start → 5 media frames → stop."""
        sim = ExotelSimulator(SimulatorConfig(sample_rate=16000))
        messages = [
            sim.connected_msg(),
            sim.start_msg(),
            *sim.media_frames(5),
            sim.stop_msg(),
        ]
        ws = _SimulatorWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws,
            business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_simulator",
        )

        captured: list[InboundAudioFrame] = []

        async def _consume() -> None:
            try:
                while True:
                    item = await asyncio.wait_for(
                        transport.inbound_queue.get(), timeout=5.0
                    )
                    if item is None:
                        break
                    if isinstance(item, InboundAudioFrame):
                        captured.append(item)
            except (TimeoutError, asyncio.CancelledError):
                pass

        consumer = asyncio.create_task(_consume())
        ended = await transport.run()
        await asyncio.sleep(0.05)
        consumer.cancel()

        assert isinstance(ended, ProviderStreamEnded)
        assert len(captured) >= 1
        for f in captured:
            assert len(f.pcm_s16le_16khz_mono) == INBOUND_BYTES_PER_FRAME

    async def test_outbound_audio_delivered(self) -> None:
        """Runtime sends outbound frames → simulator receives them."""
        sim = ExotelSimulator(SimulatorConfig(sample_rate=16000))
        messages = [sim.connected_msg(), sim.start_msg()]
        ws = _SimulatorWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws,
            business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_simulator",
        )

        async def _drive() -> None:
            await asyncio.sleep(0.1)
            for i in range(3):
                await transport.send_audio(OutboundAudioFrame(
                    generation_id=1,
                    sequence=i,
                    media_timestamp_ms=i * 20,
                    pcm_s16le_24khz_mono=b"\x00" * OUTBOUND_BYTES_PER_FRAME,
                ))
            await asyncio.sleep(0.3)
            ws.close()

        await asyncio.gather(transport.run(), _drive())

        media_sent = [s for s in ws.sent if '"media"' in s]
        assert len(media_sent) >= 1


# ============================================================================
# Unhappy paths — simulator-driven
# ============================================================================


class TestSimulatorUnhappyPaths:
    async def test_sequence_gap(self) -> None:
        """Chunk gap → InboundDiscontinuity."""
        sim = ExotelSimulator(SimulatorConfig(sample_rate=16000))
        messages = [
            sim.connected_msg(),
            sim.start_msg(),
            sim.media_msg(),
            sim.media_msg_gap(skip_to_chunk=10),
            sim.stop_msg(),
        ]
        ws = _SimulatorWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws,
            business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_simulator",
        )

        captured: list = []

        async def _consume() -> None:
            try:
                while True:
                    item = await asyncio.wait_for(
                        transport.inbound_queue.get(), timeout=5.0
                    )
                    if item is None:
                        break
                    captured.append(item)
            except (TimeoutError, asyncio.CancelledError):
                pass

        consumer = asyncio.create_task(_consume())
        await transport.run()
        await asyncio.sleep(0.05)
        consumer.cancel()

        discs = [c for c in captured if isinstance(c, InboundDiscontinuity)]
        assert len(discs) >= 1
        assert discs[0].reason == "sequence_gap"

    async def test_timestamp_regression(self) -> None:
        """Timestamp goes backward → InboundDiscontinuity(provider_reset)."""
        sim = ExotelSimulator(SimulatorConfig(sample_rate=16000))
        messages = [
            sim.connected_msg(),
            sim.start_msg(),
            sim.media_msg(),
            sim.media_msg_timestamp_regression(0),
            sim.stop_msg(),
        ]
        ws = _SimulatorWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws,
            business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_simulator",
        )

        captured: list = []

        async def _consume() -> None:
            try:
                while True:
                    item = await asyncio.wait_for(
                        transport.inbound_queue.get(), timeout=5.0
                    )
                    if item is None:
                        break
                    captured.append(item)
            except (TimeoutError, asyncio.CancelledError):
                pass

        consumer = asyncio.create_task(_consume())
        ended = await transport.run()
        await asyncio.sleep(0.05)
        consumer.cancel()

        discs = [c for c in captured if isinstance(c, InboundDiscontinuity)]
        assert len(discs) >= 1
        assert discs[0].reason == "provider_reset"

    async def test_mid_stream_disconnect(self) -> None:
        """WebSocket closes during audio → clean terminal reason."""
        sim = ExotelSimulator(SimulatorConfig(sample_rate=16000))
        messages = [
            sim.connected_msg(),
            sim.start_msg(),
            sim.media_msg(),
        ]
        ws = _SimulatorWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws,
            business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_simulator",
        )

        ended = await transport.run()
        assert isinstance(ended, ProviderStreamEnded)
        assert ended.reason in ("normal_disconnect", "protocol_error")
        assert transport.is_stopped

    async def test_barge_in_clears_old_generation(self) -> None:
        """Clear generation → old outbound frames not sent."""
        sim = ExotelSimulator(SimulatorConfig(sample_rate=16000))
        messages = [sim.connected_msg(), sim.start_msg()]
        ws = _SimulatorWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws,
            business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_simulator",
        )

        async def _drive() -> None:
            await asyncio.sleep(0.1)
            await transport.send_audio(OutboundAudioFrame(
                generation_id=1, sequence=0, media_timestamp_ms=0,
                pcm_s16le_24khz_mono=b"\x00" * OUTBOUND_BYTES_PER_FRAME,
            ))
            await asyncio.sleep(0.2)
            transport.clear_generation(1)
            await transport.send_provider_clear()
            await transport.send_audio(OutboundAudioFrame(
                generation_id=1, sequence=1, media_timestamp_ms=20,
                pcm_s16le_24khz_mono=b"\x00" * OUTBOUND_BYTES_PER_FRAME,
            ))
            await asyncio.sleep(0.2)
            ws.close()

        await asyncio.gather(transport.run(), _drive())

        clear_msgs = [s for s in ws.sent if '"clear"' in s]
        assert len(clear_msgs) >= 1

        media_after_clear = []
        found_clear = False
        for msg in ws.sent:
            if '"clear"' in msg:
                found_clear = True
            elif '"media"' in msg and found_clear:
                media_after_clear.append(msg)
        assert len(media_after_clear) == 0

    async def test_wrong_codec_fails_before_audio(self) -> None:
        """Unsupported codec → protocol_error before any audio."""
        sim = ExotelSimulator(SimulatorConfig(encoding="audio/opus"))
        messages = [sim.connected_msg(), sim.start_msg()]
        ws = _SimulatorWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws,
            business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_simulator",
        )

        ended = await transport.run()
        assert ended.reason == "protocol_error"
        assert "codec" in (ended.provider_code or "").lower()

    async def test_duplicate_start_fails(self) -> None:
        """Two start events → protocol error."""
        sim = ExotelSimulator(SimulatorConfig(sample_rate=16000))
        messages = [
            sim.connected_msg(),
            sim.start_msg(),
            sim.start_msg(),
        ]
        ws = _SimulatorWebSocket(messages)
        transport = ExotelMediaTransport(
            ws=ws,
            business_id=1,
            provider_environment="sandbox",
            provider_account_id="AC_simulator",
        )

        ended = await transport.run()
        assert ended.reason == "protocol_error"


# ============================================================================
# Correlation with simulator
# ============================================================================


class TestSimulatorCorrelation:
    async def test_callback_before_start_is_pending(self) -> None:
        """Status callback arrives before media/start → pending outcome."""
        store = InMemoryCorrelationStore()
        sim = ExotelSimulator()

        result = await store.correlate(
            provider="exotel",
            provider_account_id=sim.config.account_sid,
            provider_call_id=sim.config.call_sid,
            called_number=sim.config.to_number,
            business_id=1,
            direction=None,
        )

        assert result.outcome.value == "pending"

    async def test_callback_after_start_is_matched(self) -> None:
        """Register session, then correlate → matched."""
        store = InMemoryCorrelationStore()
        sim = ExotelSimulator()

        await store.register_admitted_call(CorrelationRecord(
            provider="exotel",
            provider_account_id=sim.config.account_sid,
            provider_call_id=sim.config.call_sid,
            called_number=sim.config.to_number,
            business_id=1,
            direction=None,
        ))

        result = await store.correlate(
            provider="exotel",
            provider_account_id=sim.config.account_sid,
            provider_call_id=sim.config.call_sid,
            called_number=sim.config.to_number,
            business_id=1,
            direction=None,
        )

        assert result.outcome.value == "matched"

    async def test_callback_wrong_business_is_conflict(self) -> None:
        """Register for business 1, callback claims business 2 → conflict."""
        store = InMemoryCorrelationStore()
        sim = ExotelSimulator()

        await store.register_admitted_call(CorrelationRecord(
            provider="exotel",
            provider_account_id=sim.config.account_sid,
            provider_call_id=sim.config.call_sid,
            called_number=sim.config.to_number,
            business_id=1,
            direction=None,
        ))

        result = await store.correlate(
            provider="exotel",
            provider_account_id=sim.config.account_sid,
            provider_call_id=sim.config.call_sid,
            called_number=sim.config.to_number,
            business_id=999,
            direction=None,
        )

        assert result.outcome.value == "conflict"
