"""End-to-end entrypoint tests with mock transport.

Invokes the actual run_voice_session entrypoint through a mock
MediaPort and verifies greeting, turn processing, terminal stop,
resource cleanup, and event delivery.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timezone
from typing import Any

import pytest

from fonely.voice.config import VoiceSessionConfig, SessionLimits
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
        now_utc=local.astimezone(timezone.utc),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, 10),
        day_of_week="monday",
    )


class MockMedia:
    def __init__(self, audio_chunks: list[bytes]):
        self._chunks = list(audio_chunks)
        self._idx = 0
        self.sent_audio: list[bytes] = []
        self.events: list[dict[str, Any]] = []
        self.closed = False

    async def receive_audio(self) -> bytes | None:
        if self._idx >= len(self._chunks):
            return None
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk

    async def send_audio(self, audio: bytes) -> None:
        self.sent_audio.append(audio)

    async def send_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    async def close(self) -> None:
        self.closed = True


class MockSTT:
    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self._idx = 0
        self.closed = False

    async def transcribe(self, audio: bytes) -> str:
        if self._idx >= len(self._texts):
            return ""
        t = self._texts[self._idx]
        self._idx += 1
        return t

    async def close(self):
        self.closed = True


class MockLLM:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._idx = 0
        self.closed = False

    async def generate(self, system: str, messages: list[dict[str, str]]) -> str:
        if self._idx >= len(self._responses):
            return ""
        r = self._responses[self._idx]
        self._idx += 1
        return r

    async def close(self):
        self.closed = True


class MockTTS:
    def __init__(self):
        self.texts: list[str] = []
        self.closed = False

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        return b"\x00" * len(text)

    async def close(self):
        self.closed = True


class MockAvailability:
    def __init__(self):
        self.queries: list[AvailabilityQuery] = []

    async def query_day_availability(self, query: AvailabilityQuery) -> DayAvailability:
        self.queries.append(query)
        return DayAvailability(
            business_date=query.target_date,
            day_of_week="monday",
            is_operating_day=True,
            is_exception_day=False,
            operating_hours=((time(10, 0), time(13, 0)),),
            available_slots=(
                AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "consultation"),
            ),
        )


class TestEntrypointFullSession:
    @pytest.mark.asyncio
    async def test_greeting_sent(self):
        media = MockMedia([b"audio1"])
        stt = MockSTT(["Fee எவ்வளவு?"])
        llm = MockLLM(["Consultation ₹300."])
        tts = MockTTS()

        summary = await run_voice_session(
            VoiceSessionConfig(session_id="ep-1", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
        )

        assert any(e["type"] == "greeting" for e in media.events)
        assert len(media.sent_audio) >= 2  # greeting + response
        assert stt.closed
        assert llm.closed
        assert tts.closed
        assert media.closed

    @pytest.mark.asyncio
    async def test_turn_events_emitted(self):
        media = MockMedia([b"a1", b"a2"])
        stt = MockSTT(["Hello", "Thanks"])
        llm = MockLLM(["Hi!", "Bye!"])
        tts = MockTTS()

        await run_voice_session(
            VoiceSessionConfig(session_id="ep-2", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
        )

        turn_events = [e for e in media.events if e["type"] == "turn_complete"]
        assert len(turn_events) == 2
        assert all(e["allowed"] for e in turn_events)

    @pytest.mark.asyncio
    async def test_terminal_stops_session(self):
        media = MockMedia([b"a"] * 5)
        stt = MockSTT(["q1", "q2", "q3", "q4", "q5"])
        llm = MockLLM(["r1", "r2", "r3", "r4", "r5"])
        tts = MockTTS()
        limits = SessionLimits(max_turns=2, max_duration_seconds=600, idle_timeout_seconds=300)
        config = VoiceSessionConfig(session_id="ep-3", business_id=1, limits=limits)

        summary = await run_voice_session(
            config,
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
        )

        terminal_events = [e for e in media.events if e["type"] == "session_terminal"]
        assert len(terminal_events) == 1
        assert terminal_events[0]["reason"] == "max_turns"

    @pytest.mark.asyncio
    async def test_consequential_blocked_no_audio(self):
        media = MockMedia([b"a1"])
        stt = MockSTT(["Confirm booking"])
        llm = MockLLM(["Booking confirmed for tomorrow."])
        tts = MockTTS()

        await run_voice_session(
            VoiceSessionConfig(session_id="ep-4", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
        )

        turn_events = [e for e in media.events if e["type"] == "turn_complete"]
        assert len(turn_events) == 1
        assert not turn_events[0]["allowed"]
        # Greeting audio + NO response audio (blocked)
        assert len(tts.texts) == 1  # Only greeting

    @pytest.mark.asyncio
    async def test_availability_queried(self):
        avail = MockAvailability()
        media = MockMedia([b"a1"])
        stt = MockSTT(["இன்னைக்கு doctor free-ஆ?"])
        llm = MockLLM(["Dr. Priya 10:00 available."])
        tts = MockTTS()

        await run_voice_session(
            VoiceSessionConfig(session_id="ep-5", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
            availability_port=avail,
        )

        assert len(avail.queries) >= 2  # init + turn
        assert avail.queries[-1].business_timezone == "Asia/Kolkata"

    @pytest.mark.asyncio
    async def test_disconnect_closes_cleanly(self):
        media = MockMedia([])  # Immediate disconnect
        stt = MockSTT([])
        llm = MockLLM([])
        tts = MockTTS()

        summary = await run_voice_session(
            VoiceSessionConfig(session_id="ep-6", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
        )

        assert summary["total_turns"] == 0
        assert media.closed
        assert stt.closed

    @pytest.mark.asyncio
    async def test_summary_includes_usage(self):
        media = MockMedia([b"a1"])
        stt = MockSTT(["Hello"])
        llm = MockLLM(["Hi!"])
        tts = MockTTS()

        summary = await run_voice_session(
            VoiceSessionConfig(session_id="ep-7", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            media=media,
            stt=stt,
            llm=llm,
            tts=tts,
        )

        assert summary["total_stt_calls"] == 1
        assert summary["total_llm_calls"] == 1
        assert summary["total_tts_bytes"] > 0
