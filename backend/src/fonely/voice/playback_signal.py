"""A one-shot playback-complete signal for the notice.

The enforced open order must wait for the notice to ACTUALLY finish playing
before it persists evidence and opens capture — not sleep a guessed duration.
Pipecat emits ``BotStoppedSpeakingFrame`` when the bot's synthesized speech has
finished playing out. This processor watches for the FIRST such frame and
resolves an ``asyncio.Event``; ``await_complete`` awaits it.

It is one-shot on purpose: it signals the end of the NOTICE playback (the first
bot utterance of the call). The greeting that plays afterwards is a separate
utterance the open order does not gate on, so re-arming would be wrong — a
second BotStoppedSpeakingFrame from the greeting must not be mistaken for the
notice's. The processor passes every frame through untouched; it only observes.

``await_complete`` takes a timeout so a notice that never signals completion
(a wedged TTS) fails closed rather than hanging the open sequence forever — the
caller of the open order treats a False return as a playback failure and keeps
capture shut.
"""

from __future__ import annotations

import asyncio

from pipecat.frames.frames import BotStoppedSpeakingFrame, Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class NoticePlaybackSignal(FrameProcessor):
    """Resolves once the first ``BotStoppedSpeakingFrame`` passes through.

    Insert it downstream of the transport output so it sees the bot-stopped
    frame after the notice plays. ``await_complete(timeout)`` returns True if
    the notice finished within the timeout, False otherwise (fail closed).
    """

    def __init__(self) -> None:
        super().__init__()
        self._done = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._done.set()
        await self.push_frame(frame, direction)

    async def await_complete(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for notice playback to finish.

        Returns False on timeout so the open order fails closed (a notice we
        cannot confirm played is treated as not played — STT stays shut)."""
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True
