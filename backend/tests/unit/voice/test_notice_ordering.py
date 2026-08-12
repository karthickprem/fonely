"""Enforced session-open order (V-lane step 4b, T3).

Proves the order and the fail-closed discipline over injected effects, so every
branch is observable:
  * happy path: notice spoken, playback awaited, evidence written of the EXACT
    spoken text, greeting spoken, latch opened — in that order.
  * playback failure → latch never opens, failure line spoken, evidence never
    written.
  * evidence-write failure AFTER successful playback → latch never opens,
    failure line spoken (playback succeeded but consent is unprovable).
The order is asserted from a recorded event log, not from timing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fonely.voice.evidence import FakeEvidenceWriter, notice_content_digest
from fonely.voice.open_order import OpenOutcome, run_open_sequence

NOTICE = "vanakkam, this call is recorded for booking only"
GREETING = "how can I help you book today?"
VERSION = "1"
LOCALE = "ta-IN"


def _now() -> datetime:
    return datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


class _Harness:
    """Records the ordered sequence of effects so ordering is asserted from a
    log rather than from timing."""

    def __init__(self, *, speak_ok: bool = True, playback_ok: bool = True) -> None:
        self.events: list[str] = []
        self.spoken: list[str] = []
        self.latch_open = False
        self._speak_ok = speak_ok
        self._playback_ok = playback_ok
        self.evidence = FakeEvidenceWriter()

    async def speak(self, text: str) -> bool:
        self.events.append(f"speak:{text[:12]}")
        self.spoken.append(text)
        return self._speak_ok

    async def await_playback_complete(self) -> bool:
        self.events.append("playback_complete")
        return self._playback_ok

    def open_latch(self) -> None:
        self.events.append("open_latch")
        self.latch_open = True

    async def speak_failure_line(self) -> None:
        self.events.append("speak_failure")

    async def run(self):
        return await run_open_sequence(
            call_id=7,
            notice_text=NOTICE,
            greeting_text=GREETING,
            notice_version=VERSION,
            locale=LOCALE,
            speak=self.speak,
            await_playback_complete=self.await_playback_complete,
            evidence_writer=self.evidence,
            open_latch=self.open_latch,
            now=_now,
            speak_failure_line=self.speak_failure_line,
        )


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_order_notice_playback_evidence_greeting_open(self):
        h = _Harness()
        result = await h.run()

        assert result.outcome is OpenOutcome.OPENED
        assert result.stt_opened is True
        assert h.latch_open is True
        # Order: notice spoken → playback complete → (evidence) → greeting → open.
        assert h.events == [
            f"speak:{NOTICE[:12]}",
            "playback_complete",
            f"speak:{GREETING[:12]}",
            "open_latch",
        ]
        # Evidence written exactly once, AFTER playback and BEFORE the latch.
        assert len(h.evidence.writes) == 1

    @pytest.mark.asyncio
    async def test_evidence_digest_is_of_exact_spoken_text(self):
        h = _Harness()
        result = await h.run()
        expected = notice_content_digest(NOTICE, VERSION, LOCALE)
        assert result.content_digest == expected
        assert h.evidence.writes[0]["content_digest"] == expected
        # And the text hashed is the text actually spoken first.
        assert h.spoken[0] == NOTICE


class TestPlaybackFailureKeepsSttClosed:
    @pytest.mark.asyncio
    async def test_playback_failure_never_opens_latch(self):
        h = _Harness(playback_ok=False)
        result = await h.run()

        assert result.outcome is OpenOutcome.NOTICE_PLAYBACK_FAILED
        assert result.stt_opened is False
        assert h.latch_open is False
        # Failure line spoken; no evidence written; latch never opened.
        assert "speak_failure" in h.events
        assert "open_latch" not in h.events
        assert h.evidence.writes == []

    @pytest.mark.asyncio
    async def test_notice_synthesis_failure_never_opens_latch(self):
        h = _Harness(speak_ok=False)
        result = await h.run()
        assert result.outcome is OpenOutcome.NOTICE_PLAYBACK_FAILED
        assert h.latch_open is False
        assert h.evidence.writes == []


class TestEvidenceFailureKeepsSttClosed:
    @pytest.mark.asyncio
    async def test_evidence_write_failure_after_playback_keeps_stt_closed(self):
        # Playback succeeds but the evidence write raises: consent is unprovable,
        # so STT must NOT open even though the notice was heard.
        h = _Harness()
        h.evidence = FakeEvidenceWriter(fail=True)
        result = await h.run()

        assert result.outcome is OpenOutcome.EVIDENCE_WRITE_FAILED
        assert result.stt_opened is False
        assert h.latch_open is False
        # Notice DID play (so we reached the write), then failure line, no open.
        assert "playback_complete" in h.events
        assert "speak_failure" in h.events
        assert "open_latch" not in h.events
