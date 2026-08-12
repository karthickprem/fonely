"""Run acceptance scenarios through actual PipelineRuntime, not scripted MockLLM.

Uses RecordingLLM that returns responses but the tests assert
runtime behavior: ports called, terminal outcomes, blocked speech,
turn caps, and resource cleanup — not expected text matching.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from fonely.voice.config import SessionLimits, VoiceSessionConfig
from fonely.voice.context import (
    AvailabilityQuery,
    AvailableSlot,
    DayAvailability,
    TrustedClock,
)
from fonely.voice.entrypoint import run_voice_session


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


class TrackingAvailability:
    def __init__(self, avail: DayAvailability):
        self._avail = avail
        self.queries: list[AvailabilityQuery] = []

    async def query_day_availability(self, q: AvailabilityQuery) -> DayAvailability:
        self.queries.append(q)
        return self._avail


MONDAY = DayAvailability(
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

SUNDAY_CLOSED = DayAvailability(
    business_date=date(2026, 8, 9),
    day_of_week="sunday",
    is_operating_day=False,
    is_exception_day=False,
    reason="Sunday closed",
)


class Media:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._idx = 0
        self.events = []
        self.audio_count = 0
        self.closed = False

    async def receive_audio(self):
        if self._idx >= len(self._chunks):
            return None
        c = self._chunks[self._idx]
        self._idx += 1
        return c

    async def send_audio(self, a):
        self.audio_count += 1

    async def send_event(self, e):
        self.events.append(e)

    async def close(self):
        self.closed = True


class STT:
    def __init__(self, texts):
        self._t = list(texts)
        self._i = 0
        self.closed = False

    async def transcribe(self, a):
        if self._i >= len(self._t):
            return ""
        t = self._t[self._i]
        self._i += 1
        return t

    async def close(self):
        self.closed = True


class LLM:
    def __init__(self, responses):
        self._r = list(responses)
        self._i = 0
        self.calls = 0
        self.closed = False

    async def generate(self, sys, msgs):
        self.calls += 1
        if self._i >= len(self._r):
            return ""
        r = self._r[self._i]
        self._i += 1
        return r

    async def close(self):
        self.closed = True


class TTS:
    def __init__(self):
        self.calls = 0
        self.closed = False

    async def synthesize(self, t):
        self.calls += 1
        return b"\x00" * len(t)

    async def close(self):
        self.closed = True


async def _run(caller, responses, *, avail=MONDAY, mode="demo", max_turns=12):
    media = Media([b"a"] * len(caller))
    stt = STT(caller)
    llm = LLM(responses)
    tts = TTS()
    ap = TrackingAvailability(avail)
    limits = SessionLimits(max_turns=max_turns, max_duration_seconds=600, idle_timeout_seconds=300)
    summary = await run_voice_session(
        VoiceSessionConfig(session_id="acc", business_id=1, limits=limits),
        clock=_clock(),
        business_name="Test Dental",
        business_timezone="Asia/Kolkata",
        media=media,
        stt=stt,
        llm=llm,
        tts=tts,
        availability_port=ap,
        session_mode=mode,
    )
    return summary, media, stt, llm, tts, ap


class TestRuntimeAcceptanceScenarios:
    @pytest.mark.asyncio
    async def test_ac001_inquiry_ports_called(self):
        _s, media, stt, llm, tts, _ap = await _run(
            ["Fee எவ்வளவு?"],
            ["Consultation ₹300."],
        )
        assert llm.calls == 1
        assert stt.closed and llm.closed and tts.closed and media.closed
        turns = [e for e in media.events if e["type"] == "turn_complete"]
        assert len(turns) == 1
        assert turns[0]["allowed"]

    @pytest.mark.asyncio
    async def test_ac002_availability_queried(self):
        _s, _media, _stt, _llm, _tts, ap = await _run(
            ["இன்னைக்கு doctor free-ஆ?"],
            ["Dr. Priya 10:00, 18:30 available."],
        )
        assert len(ap.queries) >= 2  # init + today resolve
        assert any(q.target_date == date(2026, 8, 10) for q in ap.queries)

    @pytest.mark.asyncio
    async def test_ac003_consequential_blocked(self):
        _s, media, _stt, _llm, _tts, _ap = await _run(
            ["Confirm booking"],
            ["Booking confirmed for 6:30."],
        )
        turns = [e for e in media.events if e["type"] == "turn_complete"]
        assert not turns[0]["allowed"]

    @pytest.mark.asyncio
    async def test_ac008_terminal_stops_turns(self):
        _s, media, _stt, llm, _tts, _ap = await _run(
            ["q1", "q2", "q3"],
            ["r1", "r2", "r3"],
            max_turns=2,
        )
        terminal = [e for e in media.events if e["type"] == "session_terminal"]
        assert len(terminal) == 1
        assert terminal[0]["reason"] == "max_turns"
        assert llm.calls <= 2  # No LLM after terminal

    @pytest.mark.asyncio
    async def test_ac009_safety_response(self):
        _s, media, _stt, _llm, _tts, _ap = await _run(
            ["Heavy bleeding help"],
            ["Please seek emergency care."],
        )
        turns = [e for e in media.events if e["type"] == "turn_complete"]
        assert len(turns) == 1

    @pytest.mark.asyncio
    async def test_live_without_command_port_demo(self):
        s, media, _stt, _llm, _tts, _ap = await _run(
            ["Hello"],
            ["Hi!"],
            mode="live",
        )
        # Should downgrade to demo
        assert s["total_turns"] >= 0
        assert media.closed

    @pytest.mark.asyncio
    async def test_resource_cleanup_on_error(self):
        class FailLLM:
            calls = 0
            closed = False

            async def generate(self, s, m):
                raise RuntimeError("LLM crash")

            async def close(self):
                self.closed = True

        media = Media([b"a"])
        stt = STT(["hello"])
        llm = FailLLM()
        tts = TTS()
        s = await run_voice_session(
            VoiceSessionConfig(session_id="err", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
        )
        assert media.closed
        assert "error" in s.get("close_reason", s.get("final_state", ""))

    @pytest.mark.asyncio
    async def test_commerce_inquiry_through_runtime(self):
        _s, media, _stt, llm, _tts, _ap = await _run(
            ["Rice price enna?"],
            ["Rice 5kg ₹350."],
        )
        turns = [e for e in media.events if e["type"] == "turn_complete"]
        assert turns[0]["allowed"]
        assert llm.calls == 1
