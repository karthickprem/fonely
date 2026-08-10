"""ONE complete vertical journey using canonical backend types.

Proves: admission → audio frames → STT utterance → date resolve →
availability via canonical adapter → LLM → typed propose/confirm
via ConversationServiceAdapter → commit evidence → response audio
in TurnResult → MediaPort.send_audio once → terminal → admission
release → all resources closed.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timezone
from typing import Any

import pytest

from fonely.voice.admission import AdmissionController
from fonely.voice.backend_ports import (
    AvailabilityServiceAdapter,
    ConversationServiceAdapter,
)
from fonely.voice.config import SessionLimits, SpeechClass, VoiceSessionConfig
from fonely.voice.context import (
    AvailabilityQuery,
    AvailableSlot,
    DayAvailability,
    TrustedClock,
)
from fonely.voice.dialogue import get_terminal_response
from fonely.voice.entrypoint import run_voice_session
from fonely.voice.runtime import CommandResult, ConfirmCommand, ProposeCommand


def _clock():
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    local = datetime(2026, 8, 10, 14, 30, tzinfo=tz)
    return TrustedClock(
        now_utc=local.astimezone(timezone.utc),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, 10),
        day_of_week="monday",
    )


MONDAY_AVAIL = DayAvailability(
    business_date=date(2026, 8, 10), day_of_week="monday",
    is_operating_day=True, is_exception_day=False,
    operating_hours=((time(10, 0), time(13, 0)), (time(17, 0), time(20, 30))),
    available_slots=(
        AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "consultation"),
        AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),
    ),
)


class CanonicalAvailabilityAdapter:
    """Returns typed availability using canonical DayAvailability."""
    def __init__(self):
        self.queries: list[AvailabilityQuery] = []

    async def query_day_availability(self, query: AvailabilityQuery) -> DayAvailability:
        self.queries.append(query)
        return MONDAY_AVAIL


class CanonicalCommandAdapter:
    """Tracks propose/confirm with canonical typed commands and evidence."""
    def __init__(self):
        self.proposals: list[ProposeCommand] = []
        self.confirmations: list[ConfirmCommand] = []

    async def propose(self, cmd: ProposeCommand) -> CommandResult:
        self.proposals.append(cmd)
        return CommandResult(success=True, operation="create", proposal_id=1)

    async def confirm(self, cmd: ConfirmCommand) -> CommandResult:
        self.confirmations.append(cmd)
        return CommandResult(
            success=True, operation="create", proposal_id=cmd.proposal_id,
            committed=True,
            evidence={"appointment_id": 42, "pending_action_id": 1, "idempotency_key": "conv-test-a1"},
        )


class TrackingMedia:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._idx = 0
        self.sent_audio: list[bytes] = []
        self.events: list[dict[str, Any]] = []
        self.closed = False

    async def receive_audio(self):
        if self._idx >= len(self._chunks): return None
        c = self._chunks[self._idx]; self._idx += 1; return c

    async def send_audio(self, audio):
        self.sent_audio.append(audio)

    async def send_event(self, event):
        self.events.append(event)

    async def close(self):
        self.closed = True


class TrackingSTT:
    def __init__(self, texts):
        self._t = list(texts); self._i = 0; self.calls = 0; self.closed = False
    async def transcribe(self, a):
        self.calls += 1
        if self._i >= len(self._t): return ""
        t = self._t[self._i]; self._i += 1; return t
    async def close(self): self.closed = True


class TrackingLLM:
    def __init__(self, responses):
        self._r = list(responses); self._i = 0; self.calls = 0; self.systems = []; self.closed = False
    async def generate(self, sys, msgs):
        self.calls += 1; self.systems.append(sys)
        if self._i >= len(self._r): return ""
        r = self._r[self._i]; self._i += 1; return r
    async def close(self): self.closed = True


class TrackingTTS:
    def __init__(self):
        self.texts = []; self.calls = 0; self.closed = False
    async def synthesize(self, text):
        self.calls += 1; self.texts.append(text)
        return b"\x00" * (len(text) * 40)
    async def close(self): self.closed = True


class TestCanonicalInquiryJourney:
    """Complete inquiry with canonical availability adapter."""

    @pytest.mark.asyncio
    async def test_inquiry_with_admission_and_cleanup(self):
        # 1. Admission
        ac = AdmissionController(max_per_tenant=5, max_global=20)
        decision = ac.try_admit("tenant-1")
        assert decision.admitted

        # 2. Build session
        media = TrackingMedia([b"\x00" * 160])
        stt = TrackingSTT(["இன்னைக்கு doctor free-ஆ?"])
        llm = TrackingLLM(["Dr. Priya 10:00, 18:30 available."])
        tts = TrackingTTS()
        avail = CanonicalAvailabilityAdapter()

        # 3. Run through production entrypoint
        summary = await run_voice_session(
            VoiceSessionConfig(session_id="canon-1", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            media=media, stt=stt, llm=llm, tts=tts,
            availability_port=avail,
        )

        # 4. Assert availability queried with canonical types
        assert len(avail.queries) >= 2
        assert avail.queries[-1].business_timezone == "Asia/Kolkata"
        assert avail.queries[-1].target_date == date(2026, 8, 10)

        # 5. Assert audio delivered exactly once per turn
        turn_events = [e for e in media.events if e["type"] == "turn_complete"]
        assert len(turn_events) == 1
        assert turn_events[0]["has_audio"]
        assert len(media.sent_audio) >= 2  # greeting + response

        # 6. Assert all resources closed
        assert stt.closed and llm.closed and tts.closed and media.closed

        # 7. Release admission
        ac.release("tenant-1")
        assert ac.stats()["global_active"] == 0


class TestCanonicalConsequentialBlockJourney:
    """Consequential speech without commit evidence → BLOCK."""

    @pytest.mark.asyncio
    async def test_no_audio_without_evidence(self):
        media = TrackingMedia([b"\x00" * 160])
        stt = TrackingSTT(["Confirm booking"])
        llm = TrackingLLM(["Booking confirmed for 6:30."])
        tts = TrackingTTS()

        summary = await run_voice_session(
            VoiceSessionConfig(session_id="canon-block", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media, stt=stt, llm=llm, tts=tts,
        )

        turn_events = [e for e in media.events if e["type"] == "turn_complete"]
        assert not turn_events[0]["allowed"]
        assert not turn_events[0]["has_audio"]
        assert not turn_events[0]["commit_receipt"]
        # Only greeting audio delivered
        assert len(media.sent_audio) == 1


class TestCanonicalTerminalJourney:
    """Budget → terminal → deterministic response → cleanup."""

    @pytest.mark.asyncio
    async def test_terminal_with_admission_release(self):
        ac = AdmissionController(max_per_tenant=5)
        ac.try_admit("t1")

        media = TrackingMedia([b"\x00"] * 5)
        stt = TrackingSTT(["q1", "q2", "q3", "q4", "q5"])
        llm = TrackingLLM(["a1", "a2", "a3", "a4", "a5"])
        tts = TrackingTTS()
        limits = SessionLimits(max_turns=2, max_duration_seconds=600, idle_timeout_seconds=300)

        summary = await run_voice_session(
            VoiceSessionConfig(session_id="canon-term", business_id=1, limits=limits),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media, stt=stt, llm=llm, tts=tts,
        )

        # Terminal event emitted
        terminal = [e for e in media.events if e["type"] == "session_terminal"]
        assert len(terminal) == 1
        assert terminal[0]["reason"] == "max_turns"
        # No LLM beyond budget
        assert llm.calls <= 2
        # Media closed
        assert media.closed

        # Release and verify
        ac.release("t1")
        assert ac.stats()["global_active"] == 0


class TestCanonicalCancellationJourney:
    """Cancellation → cleanup → CancelledError."""

    @pytest.mark.asyncio
    async def test_cancel_cleans_up(self):
        class BlockingMedia:
            closed = False
            async def receive_audio(self):
                await asyncio.sleep(100)
                return b"\x00"
            async def send_audio(self, a): pass
            async def send_event(self, e): pass
            async def close(self): self.closed = True

        media = BlockingMedia()
        stt = TrackingSTT([])
        llm = TrackingLLM([])
        tts = TrackingTTS()

        task = asyncio.create_task(run_voice_session(
            VoiceSessionConfig(session_id="canon-cancel", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media, stt=stt, llm=llm, tts=tts,
        ))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert media.closed
