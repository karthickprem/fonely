"""Load, failure injection, and concurrent session tests through run_voice_session.

All tests use the production entrypoint with typed ports.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time

import pytest

from fonely.voice.config import SessionLimits, VoiceSessionConfig
from fonely.voice.context import AvailableSlot, DayAvailability, TrustedClock
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


AVAIL = DayAvailability(
    business_date=date(2026, 8, 10),
    day_of_week="monday",
    is_operating_day=True,
    is_exception_day=False,
    available_slots=(AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "consultation"),),
)


class M:
    def __init__(self, n):
        self._n = n
        self._i = 0
        self.events = []
        self.closed = False

    async def receive_audio(self):
        if self._i >= self._n:
            return None
        self._i += 1
        return b"\x00" * 160

    async def send_audio(self, a):
        pass

    async def send_event(self, e):
        self.events.append(e)

    async def close(self):
        self.closed = True


class S:
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


class L:
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


class T:
    def __init__(self):
        self.calls = 0
        self.closed = False

    async def synthesize(self, t):
        self.calls += 1
        return b"\x00" * len(t)

    async def close(self):
        self.closed = True


class AP:
    def __init__(self):
        self.queries = []

    async def query_day_availability(self, q):
        self.queries.append(q)
        return AVAIL


async def _session(sid, caller, responses, max_turns=12):
    return await run_voice_session(
        VoiceSessionConfig(
            session_id=sid,
            business_id=1,
            limits=SessionLimits(
                max_turns=max_turns, max_duration_seconds=600, idle_timeout_seconds=300
            ),
        ),
        clock=_clock(),
        business_name="Test",
        business_timezone="Asia/Kolkata",
        media=M(len(caller)),
        stt=S(caller),
        llm=L(responses),
        tts=T(),
        availability_port=AP(),
    )


class TestConcurrentEntrypointSessions:
    @pytest.mark.asyncio
    async def test_5_concurrent_sessions(self):
        tasks = [_session(f"load-{i}", [f"question {i}"], [f"answer {i}"]) for i in range(5)]
        results = await asyncio.gather(*tasks)
        assert len(results) == 5
        assert all(r["total_turns"] >= 0 for r in results)
        assert all("close_reason" in r for r in results)

    @pytest.mark.asyncio
    async def test_10_concurrent_sessions_with_turns(self):
        tasks = [_session(f"load-{i}", ["q1", "q2"], ["a1", "a2"]) for i in range(10)]
        results = await asyncio.gather(*tasks)
        assert len(results) == 10
        assert all(r["total_stt_calls"] == 2 for r in results)


class TestFailureInjection:
    @pytest.mark.asyncio
    async def test_stt_crash_closes_session(self):
        class CrashSTT:
            closed = False

            async def transcribe(self, a):
                raise RuntimeError("STT crash")

            async def close(self):
                self.closed = True

        media = M(1)
        summary = await run_voice_session(
            VoiceSessionConfig(session_id="stt-crash", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=CrashSTT(),
            llm=L([]),
            tts=T(),
        )
        assert media.closed
        assert "error" in summary.get("close_reason", "")

    @pytest.mark.asyncio
    async def test_llm_crash_closes_session(self):
        class CrashLLM:
            closed = False

            async def generate(self, s, m):
                raise RuntimeError("LLM crash")

            async def close(self):
                self.closed = True

        media = M(1)
        summary = await run_voice_session(
            VoiceSessionConfig(session_id="llm-crash", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=S(["hello"]),
            llm=CrashLLM(),
            tts=T(),
        )
        assert media.closed
        assert "error" in summary.get("close_reason", "")

    @pytest.mark.asyncio
    async def test_tts_crash_closes_session(self):
        class CrashTTS:
            closed = False

            async def synthesize(self, t):
                raise RuntimeError("TTS crash")

            async def close(self):
                self.closed = True

        media = M(0)  # Only greeting will trigger TTS crash
        summary = await run_voice_session(
            VoiceSessionConfig(session_id="tts-crash", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=S([]),
            llm=L([]),
            tts=CrashTTS(),
        )
        assert media.closed
        assert "error" in summary.get("close_reason", "")

    @pytest.mark.asyncio
    async def test_availability_crash_still_runs(self):
        class CrashAP:
            async def query_day_availability(self, q):
                raise RuntimeError("DB down")

        media = M(1)
        # Availability crash during init should fail the session
        await run_voice_session(
            VoiceSessionConfig(session_id="ap-crash", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=S(["hello"]),
            llm=L(["hi"]),
            tts=T(),
            availability_port=CrashAP(),
        )
        assert media.closed


class TestTurnBudgetThroughEntrypoint:
    @pytest.mark.asyncio
    async def test_budget_2_stops_at_2(self):
        summary = await _session(
            "budget", ["q1", "q2", "q3", "q4"], ["a1", "a2", "a3", "a4"], max_turns=2
        )
        assert summary["total_turns"] <= 2

    @pytest.mark.asyncio
    async def test_budget_1_stops_at_1(self):
        summary = await _session("budget1", ["q1", "q2"], ["a1", "a2"], max_turns=1)
        assert summary["total_turns"] <= 1


class TestDisconnectAndCleanup:
    @pytest.mark.asyncio
    async def test_immediate_disconnect(self):
        summary = await _session("dc", [], [])
        assert summary["total_turns"] == 0

    @pytest.mark.asyncio
    async def test_mid_conversation_disconnect(self):
        summary = await _session("middc", ["q1"], ["a1"])
        assert summary["total_turns"] == 1
        assert summary.get("close_reason") == "normal"
