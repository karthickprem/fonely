"""NoticeInputLatch: the structural gate that keeps caller audio out of STT
until the DPDP notice has completed and its evidence is persisted.

The DPDP ordering requirement (notice → playback complete → evidence persisted
→ only then capture) is enforced as a GRAPH PROPERTY, not a timing race. This
FrameProcessor sits between the transport input and STT. While closed it drops
every ``InputAudioRawFrame`` — so STT physically cannot receive caller audio,
and any caller speech during the notice (barge-in) is discarded here. The
runtime calls ``open()`` exactly once, only after evidence persistence succeeds.

Critical: ``InputAudioRawFrame`` is itself a ``SystemFrame`` (same base as
``StartFrame``), so the latch must match the audio frame by its concrete type
and pass EVERYTHING else through untouched. Dropping system/control frames
indiscriminately would swallow ``StartFrame`` and the pipeline would never
initialise (the C3 "a control frame must never be lost" concern, inverted).
"""

from __future__ import annotations

from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class NoticeInputLatch(FrameProcessor):
    """Drops caller audio frames while closed; passes all other frames.

    Starts CLOSED. ``open()`` is a one-way flip performed by the runtime after
    the notice completes and its evidence is written. There is deliberately no
    ``close()`` re-latch: once capture is permitted for a call it stays
    permitted; a new call gets a new latch.
    """

    def __init__(self) -> None:
        super().__init__()
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        # Drop ONLY caller audio, and only while closed. Every other frame —
        # StartFrame, EndFrame, control, DTMF, interruption — passes untouched
        # so the pipeline initialises and downstream stages still see control.
        if isinstance(frame, InputAudioRawFrame) and not self._open:
            return
        await self.push_frame(frame, direction)
