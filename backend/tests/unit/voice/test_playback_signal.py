"""NoticePlaybackSignal: resolves on the first BotStoppedSpeakingFrame, times
out fail-closed otherwise.

The processor is driven through a real TaskManager setup so no un-awaited
coroutine leaks (the PytestUnraisableExceptionWarning class of bug) — the same
lifecycle discipline used in test_audio_runtime's no-leak test.
"""

from __future__ import annotations

import asyncio

import pytest
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    TextFrame,
)
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessorSetup,
)
from pipecat.utils.asyncio.task_manager import TaskManager

from fonely.voice.playback_signal import NoticePlaybackSignal


async def _setup(proc: NoticePlaybackSignal) -> None:
    tm = TaskManager(loop=asyncio.get_running_loop())
    await proc.setup(
        FrameProcessorSetup(
            clock=SystemClock(),
            task_manager=tm,
            pipeline_worker=None,  # type: ignore[arg-type]
        )
    )


def _capture_into(sig: NoticePlaybackSignal, captured: list[Frame]) -> None:
    async def _push(frame, direction=FrameDirection.DOWNSTREAM):
        captured.append(frame)

    sig.push_frame = _push  # type: ignore[assignment,method-assign]


class TestSignalResolves:
    @pytest.mark.asyncio
    async def test_await_returns_true_after_bot_stopped_speaking(self):
        sig = NoticePlaybackSignal()
        await _setup(sig)
        captured: list[Frame] = []
        _capture_into(sig, captured)

        await sig.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

        # Signalled → await returns True immediately.
        assert await sig.await_complete(timeout=0.5) is True
        # Every frame passed through untouched (observe-only).
        assert any(isinstance(f, BotStoppedSpeakingFrame) for f in captured)

    @pytest.mark.asyncio
    async def test_frames_pass_through_untouched(self):
        sig = NoticePlaybackSignal()
        await _setup(sig)
        captured: list[Frame] = []
        _capture_into(sig, captured)

        text = TextFrame(text="hello")
        await sig.process_frame(text, FrameDirection.DOWNSTREAM)
        assert text in captured


class TestTimeoutFailsClosed:
    @pytest.mark.asyncio
    async def test_await_returns_false_when_never_signalled(self):
        sig = NoticePlaybackSignal()
        await _setup(sig)
        # No BotStoppedSpeakingFrame ever arrives → fail closed within timeout.
        assert await sig.await_complete(timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_only_first_bot_stopped_matters_one_shot(self):
        sig = NoticePlaybackSignal()
        await _setup(sig)
        _capture_into(sig, [])

        await sig.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await sig.await_complete(timeout=0.5) is True
        # A later greeting-stop frame does not un-signal or error.
        await sig.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await sig.await_complete(timeout=0.5) is True
