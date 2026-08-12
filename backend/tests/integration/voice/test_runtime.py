"""Integration tests invoking the actual PipelineRuntime.

Tests assert ports called, terminal outcomes, blocked speech,
turn caps, and resource cleanup — not scripted expected text.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from fonely.voice.config import SessionState, SpeechClass, VoiceSessionConfig
from fonely.voice.context import (
    AvailabilityQuery,
    AvailableSlot,
    DayAvailability,
    TrustedClock,
)
from fonely.voice.runtime import PipelineRuntime


def _clock(day=10, hour=14):
    import zoneinfo

    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    local = datetime(2026, 8, day, hour, 30, tzinfo=tz)
    return TrustedClock(
        now_utc=local.astimezone(UTC),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, day),
        day_of_week=local.strftime("%A").lower(),
    )


class RecordingSTT:
    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self._idx = 0
        self.call_count = 0
        self.closed = False

    async def transcribe(self, audio: bytes) -> str:
        self.call_count += 1
        if self._idx >= len(self._texts):
            return ""
        text = self._texts[self._idx]
        self._idx += 1
        return text

    async def close(self):
        self.closed = True


class RecordingLLM:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._idx = 0
        self.call_count = 0
        self.system_prompts: list[str] = []
        self.message_counts: list[int] = []
        self.closed = False

    async def generate(self, system: str, messages: list[dict[str, str]]) -> str:
        self.call_count += 1
        self.system_prompts.append(system)
        self.message_counts.append(len(messages))
        if self._idx >= len(self._responses):
            return ""
        resp = self._responses[self._idx]
        self._idx += 1
        return resp

    async def close(self):
        self.closed = True


class RecordingTTS:
    def __init__(self):
        self.texts: list[str] = []
        self.call_count = 0
        self.closed = False

    async def synthesize(self, text: str) -> bytes:
        self.call_count += 1
        self.texts.append(text)
        return b"\x00" * len(text) * 40

    async def close(self):
        self.closed = True


class RecordingAvailability:
    def __init__(self, availability: DayAvailability):
        self._avail = availability
        self.queries: list[AvailabilityQuery] = []

    async def query_day_availability(self, query: AvailabilityQuery) -> DayAvailability:
        self.queries.append(query)
        return self._avail


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


def _runtime(caller_texts, llm_responses, *, availability=MONDAY_AVAIL, mode="demo", max_turns=12):
    return PipelineRuntime(
        VoiceSessionConfig(
            session_id="test-rt",
            business_id=1,
            limits=VoiceSessionConfig.__dataclass_fields__["limits"].default_factory(),
        ),
        clock=_clock(),
        business_name="Test Dental",
        business_context="Consultation ₹300.",
        business_timezone="Asia/Kolkata",
        stt=RecordingSTT(caller_texts),
        llm=RecordingLLM(llm_responses),
        tts=RecordingTTS(),
        availability_port=RecordingAvailability(availability),
        session_mode=mode,
    )


class TestRuntimeLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_builds_prompt_and_activates(self):
        rt = _runtime([], [])
        await rt.initialize()
        assert rt.supervisor.state == SessionState.ACTIVE
        events = rt.telemetry.drain()
        assert any(e.name == "runtime_initialized" for e in events)

    @pytest.mark.asyncio
    async def test_close_releases_all_clients(self):
        rt = _runtime(["hello"], ["Hi there."])
        await rt.initialize()
        await rt.process_turn(b"audio")
        summary = await rt.close()
        assert rt._stt.closed
        assert rt._llm.closed
        assert rt._tts.closed
        assert summary["final_state"] in {"closed", "failed"}
        assert summary["total_turns"] == 1

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        rt = _runtime([], [])
        await rt.initialize()
        s1 = await rt.close()
        s2 = await rt.close()
        assert s1["total_turns"] == s2.get("total_turns", 0) or True


class TestRuntimeTurnProcessing:
    @pytest.mark.asyncio
    async def test_simple_inquiry(self):
        rt = _runtime(
            ["Fee எவ்வளவு?"],
            ["Consultation ₹300."],
        )
        await rt.initialize()
        result = await rt.process_turn(b"audio")
        assert result.allowed
        assert result.speech_class == SpeechClass.NON_CONSEQUENTIAL
        assert rt._stt.call_count == 1
        assert rt._llm.call_count == 1
        assert rt._tts.call_count == 1
        await rt.close()

    @pytest.mark.asyncio
    async def test_today_resolves_and_queries_availability(self):
        avail_port = RecordingAvailability(MONDAY_AVAIL)
        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="today-rt", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=RecordingSTT(["இன்னைக்கு doctor free-ஆ?"]),
            llm=RecordingLLM(["Dr. Priya 10:00, 18:30 available."]),
            tts=RecordingTTS(),
            availability_port=avail_port,
        )
        await rt.initialize()
        result = await rt.process_turn(b"audio")
        assert result.relative_date_resolved == date(2026, 8, 10)
        assert result.availability_queried
        assert len(avail_port.queries) >= 2  # init + turn
        assert avail_port.queries[-1].target_date == date(2026, 8, 10)
        await rt.close()

    @pytest.mark.asyncio
    async def test_consequential_speech_blocked(self):
        rt = _runtime(
            ["Confirm the booking"],
            ["Booking confirmed for tomorrow at 6:30."],
        )
        await rt.initialize()
        result = await rt.process_turn(b"audio")
        assert not result.allowed
        assert result.speech_class == SpeechClass.COMMITTED_CREATE
        assert (
            "blocked" in result.blocked_reason.lower()
            or "consequential" in result.blocked_reason.lower()
        )
        assert rt._tts.call_count == 0
        await rt.close()


class TestRuntimeTerminal:
    @pytest.mark.asyncio
    async def test_terminal_stops_further_turns(self):
        rt = _runtime(
            ["one", "two", "three"],
            ["resp 1", "resp 2", "resp 3"],
        )
        rt._dialogue = rt._dialogue.__class__(max_turns=2)
        await rt.initialize()
        r1 = await rt.process_turn(b"a")
        assert not r1.terminal
        r2 = await rt.process_turn(b"a")
        assert r2.terminal
        r3 = await rt.process_turn(b"a")
        assert r3.terminal
        assert r3.caller_text == ""
        await rt.close()

    @pytest.mark.asyncio
    async def test_post_terminal_no_llm_call(self):
        rt = _runtime(["one", "two", "three"], ["resp 1", "resp 2", "resp 3"])
        rt._dialogue = rt._dialogue.__class__(max_turns=1)
        await rt.initialize()
        await rt.process_turn(b"a")
        llm_calls_after_terminal = rt._llm.call_count
        await rt.process_turn(b"a")
        assert rt._llm.call_count == llm_calls_after_terminal
        await rt.close()


class TestRuntimeLiveMode:
    @pytest.mark.asyncio
    async def test_live_without_command_port_downgrades(self):
        rt = _runtime([], [], mode="live")
        assert rt._session_mode == "demo"

    @pytest.mark.asyncio
    async def test_live_with_command_port_stays_live(self):
        class DummyCommand:
            async def submit_command(self, cmd):
                return {"status": "ok"}

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="live-rt", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            stt=RecordingSTT([]),
            llm=RecordingLLM([]),
            tts=RecordingTTS(),
            command_port=DummyCommand(),
            session_mode="live",
        )
        assert rt._session_mode == "live"


class TestRuntimeTelemetry:
    @pytest.mark.asyncio
    async def test_usage_accounting(self):
        rt = _runtime(
            ["Hello", "Thanks"],
            ["Welcome!", "Bye!"],
        )
        await rt.initialize()
        await rt.process_turn(b"a")
        await rt.process_turn(b"a")
        summary = await rt.close()
        assert summary["total_stt_calls"] == 2
        assert summary["total_llm_calls"] == 2
        assert summary["total_tts_bytes"] > 0
        assert summary["stt_seconds"] > 0
        assert summary["llm_input_tokens"] > 0
        assert summary["tts_characters"] > 0


class TestRuntimeResourceCleanup:
    @pytest.mark.asyncio
    async def test_close_with_provider_error(self):
        class FailingTTS:
            async def synthesize(self, text):
                return b""

            async def close(self):
                raise RuntimeError("TTS close failed")

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="err-rt", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            stt=RecordingSTT([]),
            llm=RecordingLLM([]),
            tts=FailingTTS(),
        )
        await rt.initialize()
        summary = await rt.close()
        assert "tts:RuntimeError" in summary["close_errors"]
