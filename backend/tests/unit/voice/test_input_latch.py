"""NoticeInputLatch (V-lane step 2): caller audio is suppressed until open().

Drives the processor with the established collector pattern (override push_frame,
feed via process_frame). The load-bearing assertions:
  * closed latch drops InputAudioRawFrame (STT cannot see caller audio);
  * closed latch PASSES StartFrame and other control frames (else the pipeline
    would never initialise — StartFrame is also a SystemFrame);
  * open() lets audio through.
"""

from __future__ import annotations

import pytest
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from fonely.voice.input_latch import NoticeInputLatch


class _Collector:
    def __init__(self) -> None:
        self.frames: list[Frame] = []

    async def _cb(self, frame: Frame, direction: FrameDirection) -> None:
        self.frames.append(frame)


def _audio() -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=b"\x00\x00", sample_rate=16000, num_channels=1)


async def _drive(latch: NoticeInputLatch, frame: Frame) -> _Collector:
    collector = _Collector()
    latch.push_frame = collector._cb  # type: ignore[method-assign]
    await latch.process_frame(frame, FrameDirection.DOWNSTREAM)
    return collector


class TestNoticeInputLatch:
    def test_starts_closed(self):
        assert NoticeInputLatch().is_open is False

    @pytest.mark.asyncio
    async def test_closed_drops_caller_audio(self):
        latch = NoticeInputLatch()
        collector = await _drive(latch, _audio())
        assert collector.frames == []  # audio suppressed while closed

    @pytest.mark.asyncio
    async def test_closed_passes_non_audio_frame(self):
        # A closed latch drops ONLY caller audio; every other frame passes so
        # the pipeline keeps initialising and control keeps flowing. (StartFrame
        # itself is intercepted by Pipecat's base FrameProcessor before user
        # code and needs a running TaskManager — its survival through a closed
        # latch is asserted in the assembly/transport test, not this bare
        # collector harness.)
        latch = NoticeInputLatch()
        msg = LLMTextFrame(text="passthrough")
        collector = await _drive(latch, msg)
        assert len(collector.frames) == 1
        assert collector.frames[0] is msg

    @pytest.mark.asyncio
    async def test_open_lets_audio_through(self):
        latch = NoticeInputLatch()
        latch.open()
        assert latch.is_open is True
        collector = await _drive(latch, _audio())
        assert len(collector.frames) == 1
        assert isinstance(collector.frames[0], InputAudioRawFrame)

    @pytest.mark.asyncio
    async def test_open_is_one_way(self):
        latch = NoticeInputLatch()
        latch.open()
        latch.open()  # idempotent
        assert latch.is_open is True
