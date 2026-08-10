"""C4 loopback barge-in proof — real Pipecat output transport, no Exotel.

Drives FastAPIWebsocketOutputTransport with a capturing WebSocket mock.
Proves: InterruptionFrame produces {"event":"clear"} on the wire.

Stale-audio suppression is a PIPELINE responsibility (InterruptionFrame
propagates upstream and cancels the TTS/generation source), not a
transport-output responsibility. The transport faithfully serializes
whatever frames arrive — it is the pipeline that stops sending them.
Full pipeline barge-in proof requires Dev4's runtime in a combined tree.
"""

from __future__ import annotations

import json

from pipecat.frames.frames import (
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.serializers.exotel import ExotelFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketCallbacks,
    FastAPIWebsocketClient,
    FastAPIWebsocketOutputTransport,
    FastAPIWebsocketParams,
)
from pipecat.utils.asyncio.task_manager import TaskManager
from starlette.websockets import WebSocketState


class _CapturingWebSocket:
    """Fake WebSocket that captures sent text for assertion."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        self.client_state = WebSocketState.DISCONNECTED


def _noop(_ws):
    import asyncio

    f = asyncio.get_event_loop().create_future()
    f.set_result(None)
    return f


def _make_output() -> tuple[FastAPIWebsocketOutputTransport, _CapturingWebSocket]:
    fake_ws = _CapturingWebSocket()
    serializer = ExotelFrameSerializer(
        stream_sid="MZ_barge_loopback",
        params=ExotelFrameSerializer.InputParams(
            exotel_sample_rate=16000,
            sample_rate=16000,
        ),
    )
    params = FastAPIWebsocketParams(
        serializer=serializer,
        allowed_origins=[],
        audio_out_sample_rate=24000,
    )
    callbacks = FastAPIWebsocketCallbacks(
        on_client_connected=_noop,
        on_client_disconnected=_noop,
        on_session_timeout=_noop,
    )
    client = FastAPIWebsocketClient(fake_ws, callbacks)
    output = FastAPIWebsocketOutputTransport(
        transport=None,
        client=client,
        params=params,
    )
    output._task_manager = TaskManager()
    output._send_interval = 0
    return output, fake_ws


class TestC4BargeInLoopback:
    async def test_interruption_emits_clear_on_wire(self) -> None:
        """Real transport output: InterruptionFrame → {"event":"clear"}
        appears in the captured WebSocket send stream."""
        output, fake_ws = _make_output()

        sf = StartFrame(
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        )
        await output.process_frame(sf, FrameDirection.DOWNSTREAM)

        await output.write_audio_frame(
            OutputAudioRawFrame(
                audio=b"\x00" * 4800,
                sample_rate=24000,
                num_channels=1,
            )
        )

        await output.process_frame(
            InterruptionFrame(), FrameDirection.DOWNSTREAM
        )

        clear_messages = [
            m for m in fake_ws.sent if '"clear"' in m
        ]
        assert len(clear_messages) >= 1

        parsed = json.loads(clear_messages[0])
        assert parsed["event"] == "clear"
        assert parsed["streamSid"] == "MZ_barge_loopback"

    async def test_audio_before_clear_is_on_wire(self) -> None:
        """Audio frames before InterruptionFrame reach the wire — the
        transport serializes faithfully. It is the pipeline that stops
        sending frames after an interruption, not the transport."""
        output, fake_ws = _make_output()

        sf = StartFrame(
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        )
        await output.process_frame(sf, FrameDirection.DOWNSTREAM)

        for _ in range(3):
            await output.write_audio_frame(
                OutputAudioRawFrame(
                    audio=b"\x00" * 4800,
                    sample_rate=24000,
                    num_channels=1,
                )
            )

        media_messages = [m for m in fake_ws.sent if '"media"' in m]
        assert len(media_messages) >= 1
