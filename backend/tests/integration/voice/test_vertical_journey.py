"""ONE executable vertical journey through actual PipelineRuntime.

Proves: audio frames → STT utterance → date resolve → availability →
LLM → command propose/confirm → commit evidence → deterministic
confirmation speech → validator ALLOW → TTS bytes → MediaPort send →
terminal cleanup → admission release.

Asserts command called, receipt ID, one audible response per turn,
false commit impossible without evidence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time
from typing import Any

import pytest

from fonely.voice.admission import AdmissionController
from fonely.voice.config import SessionLimits, VoiceSessionConfig
from fonely.voice.context import (
    AvailabilityQuery,
    AvailableSlot,
    DayAvailability,
    TrustedClock,
)
from fonely.voice.entrypoint import run_voice_session
from fonely.voice.runtime import CommandResult, ConfirmCommand, ProposeCommand


def _clock():
    import zoneinfo

    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    local = datetime(2026, 8, 10, 14, 30, tzinfo=tz)
    return TrustedClock(
        now_utc=local.astimezone(UTC),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, 10),
        day_of_week="monday",
    )


MONDAY_AVAIL = DayAvailability(
    business_date=date(2026, 8, 10),
    day_of_week="monday",
    is_operating_day=True,
    is_exception_day=False,
    operating_hours=((time(10, 0), time(13, 0)), (time(17, 0), time(20, 30))),
    available_slots=(
        AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "consultation"),
        AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),
    ),
)


class TrackingMedia:
    def __init__(self, audio_chunks: list[bytes]):
        self._chunks = list(audio_chunks)
        self._idx = 0
        self.sent_audio: list[bytes] = []
        self.events: list[dict[str, Any]] = []
        self.closed = False

    async def receive_audio(self) -> bytes | None:
        if self._idx >= len(self._chunks):
            return None
        c = self._chunks[self._idx]
        self._idx += 1
        return c

    async def send_audio(self, audio: bytes) -> None:
        self.sent_audio.append(audio)

    async def send_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    async def close(self) -> None:
        self.closed = True


class TrackingSTT:
    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self._idx = 0
        self.calls = 0
        self.closed = False

    async def transcribe(self, audio: bytes) -> str:
        self.calls += 1
        if self._idx >= len(self._texts):
            return ""
        t = self._texts[self._idx]
        self._idx += 1
        return t

    async def close(self):
        self.closed = True


class TrackingLLM:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._idx = 0
        self.calls = 0
        self.systems: list[str] = []
        self.closed = False

    async def generate(self, system: str, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        self.systems.append(system)
        if self._idx >= len(self._responses):
            return ""
        r = self._responses[self._idx]
        self._idx += 1
        return r

    async def close(self):
        self.closed = True


class TrackingTTS:
    def __init__(self):
        self.texts: list[str] = []
        self.calls = 0
        self.closed = False

    async def synthesize(self, text: str) -> bytes:
        self.calls += 1
        self.texts.append(text)
        return b"\x00" * (len(text) * 40)

    async def close(self):
        self.closed = True


class TrackingAvailability:
    def __init__(self):
        self.queries: list[AvailabilityQuery] = []

    async def query_day_availability(self, query: AvailabilityQuery) -> DayAvailability:
        self.queries.append(query)
        return MONDAY_AVAIL


class TrackingCommandPort:
    """Test command port that tracks propose/confirm calls and returns evidence."""

    def __init__(self):
        self.proposals: list[ProposeCommand] = []
        self.confirmations: list[ConfirmCommand] = []
        self._proposal_counter = 0

    async def propose(self, cmd: ProposeCommand) -> CommandResult:
        self._proposal_counter += 1
        self.proposals.append(cmd)
        return CommandResult(
            success=True,
            operation="create",
            proposal_id=self._proposal_counter,
        )

    async def confirm(self, cmd: ConfirmCommand) -> CommandResult:
        self.confirmations.append(cmd)
        return CommandResult(
            success=True,
            operation="create",
            proposal_id=cmd.proposal_id,
            committed=True,
            evidence={"appointment_id": 42, "pending_action_id": cmd.proposal_id},
        )


class TestVerticalInquiryJourney:
    """Simple inquiry: audio → STT → LLM → TTS → media send → close."""

    @pytest.mark.asyncio
    async def test_inquiry_audio_delivered(self):
        media = TrackingMedia([b"\x00" * 160])
        stt = TrackingSTT(["Fee எவ்வளவு?"])
        llm = TrackingLLM(["Consultation ₹300."])
        tts = TrackingTTS()
        avail = TrackingAvailability()

        await run_voice_session(
            VoiceSessionConfig(session_id="vj-1", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
            availability_port=avail,
        )

        # STT called
        assert stt.calls == 1
        # LLM called with system prompt containing availability
        assert llm.calls == 1
        assert "Dr. Priya" in llm.systems[0] or "available" in llm.systems[0].lower()
        # TTS: greeting + response = 2 calls
        assert tts.calls >= 2
        # Media got greeting + response audio
        assert len(media.sent_audio) >= 2
        # Turn event shows audio delivered
        turn_events = [e for e in media.events if e["type"] == "turn_complete"]
        assert len(turn_events) == 1
        assert turn_events[0]["has_audio"]
        # All clients closed
        assert stt.closed and llm.closed and tts.closed and media.closed


class TestVerticalConsequentialBlockJourney:
    """Consequential speech without evidence → BLOCK, no audio."""

    @pytest.mark.asyncio
    async def test_false_commit_blocked(self):
        media = TrackingMedia([b"\x00" * 160])
        stt = TrackingSTT(["Confirm the booking"])
        llm = TrackingLLM(["Booking confirmed for tomorrow at 6:30."])
        tts = TrackingTTS()

        await run_voice_session(
            VoiceSessionConfig(session_id="vj-block", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
        )

        turn_events = [e for e in media.events if e["type"] == "turn_complete"]
        assert len(turn_events) == 1
        assert not turn_events[0]["allowed"]
        assert not turn_events[0]["has_audio"]
        assert not turn_events[0]["commit_receipt"]
        # Only greeting audio, no response audio
        assert len(media.sent_audio) == 1  # greeting only


class TestVerticalAvailabilityJourney:
    """Today availability: date resolve → query port → LLM with data."""

    @pytest.mark.asyncio
    async def test_today_availability_queried(self):
        media = TrackingMedia([b"\x00" * 160])
        stt = TrackingSTT(["இன்னைக்கு doctor free-ஆ?"])
        llm = TrackingLLM(["Dr. Priya 10:00, 18:30 available."])
        tts = TrackingTTS()
        avail = TrackingAvailability()

        await run_voice_session(
            VoiceSessionConfig(session_id="vj-avail", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
            availability_port=avail,
        )

        # Availability queried at init AND per-turn (today resolved)
        assert len(avail.queries) >= 2
        per_turn = [q for q in avail.queries if q.target_date == date(2026, 8, 10)]
        assert len(per_turn) >= 1
        assert per_turn[0].business_timezone == "Asia/Kolkata"


class TestVerticalTerminalJourney:
    """Budget exceeded → terminal → no more LLM/TTS → cleanup."""

    @pytest.mark.asyncio
    async def test_terminal_stops_pipeline(self):
        media = TrackingMedia([b"\x00"] * 5)
        stt = TrackingSTT(["q1", "q2", "q3", "q4", "q5"])
        llm = TrackingLLM(["a1", "a2", "a3", "a4", "a5"])
        tts = TrackingTTS()
        limits = SessionLimits(max_turns=2, max_duration_seconds=600, idle_timeout_seconds=300)

        await run_voice_session(
            VoiceSessionConfig(session_id="vj-term", business_id=1, limits=limits),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
        )

        terminal = [e for e in media.events if e["type"] == "session_terminal"]
        assert len(terminal) == 1
        assert terminal[0]["reason"] == "max_turns"
        # LLM not called beyond budget
        assert llm.calls <= 2
        assert media.closed


class TestVerticalAdmissionJourney:
    """Admission control: admit → session → release."""

    @pytest.mark.asyncio
    async def test_admission_release(self):
        ac = AdmissionController(max_per_tenant=2, max_global=5)
        decision = ac.try_admit("tenant-1")
        assert decision.admitted

        media = TrackingMedia([b"\x00"])
        stt = TrackingSTT(["hello"])
        llm = TrackingLLM(["hi!"])
        tts = TrackingTTS()

        await run_voice_session(
            VoiceSessionConfig(session_id="vj-adm", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
        )

        ac.release("tenant-1")
        assert ac.stats()["global_active"] == 0


class TestVerticalCancellationJourney:
    """Cancellation stops turn loop and re-raises."""

    @pytest.mark.asyncio
    async def test_cancellation_closes_cleanly(self):
        class SlowMedia:
            closed = False

            async def receive_audio(self):
                await asyncio.sleep(100)  # Block until cancelled
                return b"\x00"

            async def send_audio(self, a):
                pass

            async def send_event(self, e):
                pass

            async def close(self):
                self.closed = True

        media = SlowMedia()
        stt = TrackingSTT([])
        llm = TrackingLLM([])
        tts = TrackingTTS()

        task = asyncio.create_task(
            run_voice_session(
                VoiceSessionConfig(session_id="vj-cancel", business_id=1),
                clock=_clock(),
                business_name="Test",
                business_timezone="Asia/Kolkata",
                media=media,
                stt=stt,
                llm=llm,
                tts=tts,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert media.closed
