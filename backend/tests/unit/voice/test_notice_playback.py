"""Notice-open binding to a frame queue + latch (D1/D2 fail-closed proof).

This is the frame-level analogue of test_run_call_open: the ordering itself is
proven in test_notice_ordering; here we prove the BINDING that the lab demo got
wrong. The demo enqueued the greeting frames unconditionally, so a failed
evidence write still greeted the caller and opened capture. The property under
test — the one the CEO asked to see — is that on an evidence-write failure the
GREETING frames are NOT in the queued frames and the latch never opens.

The queue is a real recorder: every enqueued frame is captured, tagged by which
line produced it, so "the greeting was never queued" is asserted against actual
recorded frames, not a self-reported flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from fonely.voice.evidence import FakeEvidenceWriter, notice_content_digest
from fonely.voice.input_latch import NoticeInputLatch
from fonely.voice.notice_playback import build_notice_open_sequence
from fonely.voice.open_order import OpenOutcome
from fonely.voice.session_open import open_session

CLINIC = "Smile Care Dental Clinic"
GREETING = "vanakkam, appointment book panna help pannalaam"
FAILURE_LINE = "sorry, technical issue, please call the clinic"
LOCALE = "ta-IN"


def _now() -> datetime:
    return datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


@dataclass
class _Frame:
    """A stand-in synthesis frame tagged with the line it was produced from, so
    the recorder can prove WHICH utterances reached the queue."""

    line: str


@dataclass
class _RecordingWorker:
    """Records every enqueued frame. ``fail_enqueue`` simulates a broken
    transport so the speak-failure branch is exercised too."""

    queued: list[_Frame] = field(default_factory=list)
    fail_enqueue: bool = False

    async def queue_frames(self, frames) -> None:
        if self.fail_enqueue:
            raise RuntimeError("transport enqueue failed")
        self.queued.extend(frames)

    def lines(self) -> list[str]:
        return [f.line for f in self.queued]


def _make_speech_frames(text: str):
    # One tagged frame per line is enough to prove presence/absence in the queue.
    return [_Frame(line=text)]


def _build(
    *,
    worker: _RecordingWorker,
    latch: NoticeInputLatch,
    evidence: FakeEvidenceWriter,
    playback_ok: bool = True,
):
    opening = open_session(clinic_name=CLINIC, greeting_text=GREETING, locale=LOCALE)

    async def await_playback_complete() -> bool:
        return playback_ok

    return (
        opening,
        build_notice_open_sequence(
            call_id=42,
            opening=opening,
            locale=LOCALE,
            queue_frames=worker.queue_frames,
            make_speech_frames=_make_speech_frames,
            await_playback_complete=await_playback_complete,
            evidence_writer=evidence,
            latch=latch,
            now=_now,
            failure_line=FAILURE_LINE,
        ),
    )


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_notice_then_greeting_queued_and_latch_opens_after_evidence(self):
        worker = _RecordingWorker()
        latch = NoticeInputLatch()
        evidence = FakeEvidenceWriter()
        opening, open_seq = _build(worker=worker, latch=latch, evidence=evidence)

        result = await open_seq()

        assert result.outcome is OpenOutcome.OPENED
        assert latch.is_open is True
        # Notice queued first, greeting second — in that order.
        assert worker.lines() == [opening.notice_text, GREETING]
        # Evidence written once, of the EXACT spoken notice text.
        assert len(evidence.writes) == 1
        assert evidence.writes[0]["content_digest"] == notice_content_digest(
            opening.notice_text, opening.notice_version, LOCALE
        )


class TestEvidenceWriteFailureIsFailClosed:
    """The D2 defect, inverted into a guard: a failed evidence write must NOT
    greet the caller and must NOT open capture."""

    @pytest.mark.asyncio
    async def test_greeting_never_queued_and_latch_closed_on_write_failure(self):
        worker = _RecordingWorker()
        latch = NoticeInputLatch()
        evidence = FakeEvidenceWriter(fail=True)  # inject the defect condition
        opening, open_seq = _build(worker=worker, latch=latch, evidence=evidence)

        result = await open_seq()

        assert result.outcome is OpenOutcome.EVIDENCE_WRITE_FAILED
        # THE ASSERTION: the greeting frame is NOT among the queued frames, and
        # capture never opened. This is what the lab demo violated.
        assert GREETING not in worker.lines()
        assert latch.is_open is False
        # The notice DID play (that's why we reached the write), then the caller
        # heard the failure line — not silence.
        assert opening.notice_text in worker.lines()
        assert FAILURE_LINE in worker.lines()
        # Nothing was recorded as persisted evidence.
        assert evidence.writes == []

    @pytest.mark.asyncio
    async def test_mutation_guard_a_passing_write_would_queue_the_greeting(self):
        # Prove the previous test can distinguish the two worlds: with the write
        # SUCCEEDING (defect absent), the greeting IS queued and the latch opens.
        # If the guard were vacuous, this would be indistinguishable.
        worker = _RecordingWorker()
        latch = NoticeInputLatch()
        _opening, open_seq = _build(worker=worker, latch=latch, evidence=FakeEvidenceWriter())

        result = await open_seq()

        assert result.outcome is OpenOutcome.OPENED
        assert GREETING in worker.lines()
        assert latch.is_open is True


class TestPlaybackFailureIsFailClosed:
    @pytest.mark.asyncio
    async def test_greeting_never_queued_when_playback_fails(self):
        worker = _RecordingWorker()
        latch = NoticeInputLatch()
        evidence = FakeEvidenceWriter()
        _opening, open_seq = _build(
            worker=worker, latch=latch, evidence=evidence, playback_ok=False
        )

        result = await open_seq()

        assert result.outcome is OpenOutcome.NOTICE_PLAYBACK_FAILED
        assert GREETING not in worker.lines()
        assert latch.is_open is False
        assert evidence.writes == []  # never reached the write


class TestBrokenTransportIsFailClosed:
    @pytest.mark.asyncio
    async def test_enqueue_failure_treated_as_playback_failure(self):
        # If the transport enqueue itself raises, speak() returns False → the
        # sequence fails closed (no evidence, latch closed) rather than raising.
        worker = _RecordingWorker(fail_enqueue=True)
        latch = NoticeInputLatch()
        evidence = FakeEvidenceWriter()
        _opening, open_seq = _build(worker=worker, latch=latch, evidence=evidence)

        result = await open_seq()

        assert result.outcome is OpenOutcome.NOTICE_PLAYBACK_FAILED
        assert latch.is_open is False
        assert evidence.writes == []
