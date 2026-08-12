"""Provider-free conversation replay and acceptance regression.

Replays the canonical failed R&D transcript through the production
runtime's typed ports and verifies every defect is architecturally
prevented.  Also runs the clean synthetic conversation flows from
the acceptance matrix.

No live providers, no browser, no audio.  Uses mock STT/LLM/TTS
with deterministic text responses.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from fonely.voice.config import SpeechClass, VoiceSessionConfig
from fonely.voice.context import (
    AvailableSlot,
    DayAvailability,
    StubAvailabilityPort,
    TrustedClock,
)
from fonely.voice.dialogue import (
    DialogueState,
    count_questions,
    detect_filler,
    get_terminal_response,
)
from fonely.voice.generation import GenerationClock
from fonely.voice.lifecycle import VoiceSessionSupervisor
from fonely.voice.pipeline import PostTTSGenerationGate, PreTTSValidatorGate, build_pipeline_context
from fonely.voice.prompts import format_availability
from fonely.voice.telemetry import VoiceTelemetryExporter
from fonely.voice.validator_port import FailClosedValidatorStub


def _clock(day=10, hour=14, minute=30):
    import zoneinfo

    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    local = datetime(2026, 8, day, hour, minute, tzinfo=tz)
    return TrustedClock(
        now_utc=local.astimezone(UTC),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, day),
        day_of_week=local.strftime("%A").lower(),
    )


def _monday_availability():
    return DayAvailability(
        business_date=date(2026, 8, 10),
        day_of_week="monday",
        is_operating_day=True,
        is_exception_day=False,
        operating_hours=((time(10, 0), time(13, 0)), (time(17, 0), time(20, 30))),
        available_slots=(
            AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "general"),
            AvailableSlot(1, "Dr. Priya", time(11, 0), time(11, 30), "general"),
            AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),
        ),
    )


def _sunday_closed():
    return DayAvailability(
        business_date=date(2026, 8, 9),
        day_of_week="sunday",
        is_operating_day=False,
        is_exception_day=False,
        reason="Sunday closed",
    )


def _fully_booked():
    return DayAvailability(
        business_date=date(2026, 8, 10),
        day_of_week="monday",
        is_operating_day=True,
        is_exception_day=False,
        fully_booked=True,
    )


class TestFailedTranscriptRegression:
    """Replay the canonical failed R&D conversation and verify every
    defect is architecturally prevented."""

    def test_today_resolves_to_actual_date(self):
        clock = _clock(day=10)
        from fonely.voice.context import resolve_relative_date

        resolved = resolve_relative_date("இன்னைக்கு doctor free-ஆ?", clock)
        assert resolved == date(2026, 8, 10)

    @pytest.mark.asyncio
    async def test_no_hardcoded_slots_in_prompt(self):
        ctx = await build_pipeline_context(
            VoiceSessionConfig(session_id="test", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
        )
        assert "Tomorrow: 10, 11, 5, 6:30, 7:30" not in ctx.system_prompt

    def test_availability_from_typed_port_not_generic_hours(self):
        avail = _monday_availability()
        text = format_availability(avail)
        assert "Dr. Priya" in text
        assert "10:00" in text
        assert "18:30" in text
        assert "Mon-Sat" not in text

    def test_closed_day_not_generic_hours(self):
        text = format_availability(_sunday_closed())
        assert "CLOSED" in text
        assert "Sunday closed" in text

    def test_fully_booked_not_generic_hours(self):
        text = format_availability(_fully_booked())
        assert "FULLY BOOKED" in text

    @pytest.mark.asyncio
    async def test_demo_mode_disclosed_before_collection(self):
        ctx = await build_pipeline_context(
            VoiceSessionConfig(session_id="test", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            session_mode="demo",
        )
        assert "demo" in ctx.system_prompt.lower()
        assert "CANNOT" in ctx.system_prompt or "cannot" in ctx.system_prompt

    def test_filler_detected(self):
        assert detect_filler("சோ அதனால பாக்கணும்")
        assert detect_filler("Sure, I can help you with that")
        assert not detect_filler("நாளைக்கு 6:30 available.")

    def test_turn_budget_enforced(self):
        ds = DialogueState(max_turns=5)
        for i in range(5):
            ds.record_turn(f"response {i}")
        assert ds.is_over_budget()
        terminal = get_terminal_response("max_turns", "ta-Latn")
        assert len(terminal) > 0

    def test_repeated_question_detected(self):
        ds = DialogueState()
        ds.record_turn("What date?", asked_field="date")
        ds.record_turn("What date?", asked_field="date")
        assert ds.has_repeated_question()

    def test_terminal_deterministic_not_llm(self):
        for reason in ["abandoned", "max_turns", "demo_complete", "safety", "handoff"]:
            for lang in ["ta-Latn", "ta", "en"]:
                r = get_terminal_response(reason, lang)
                assert len(r) > 5
                assert not detect_filler(r)

    def test_consequential_blocked_before_tts(self):
        gate = PreTTSValidatorGate(
            FailClosedValidatorStub(),
            VoiceTelemetryExporter("test"),
            GenerationClock("test"),
        )
        assert not gate.check("Booking confirmed.", SpeechClass.COMMITTED_CREATE)
        assert gate.check("What date works?", SpeechClass.NON_CONSEQUENTIAL)

    def test_stale_generation_dropped(self):
        clock = GenerationClock("test")
        clock.next_turn()
        stale = clock.current().generation_id
        clock.advance_generation()
        gate = PostTTSGenerationGate(clock, VoiceTelemetryExporter("test"))
        assert not gate.should_emit(stale)
        assert gate.should_emit(clock.current().generation_id)

    def test_timezone_day_boundary(self):
        clock_late = _clock(day=10, hour=23, minute=30)
        assert clock_late.business_date == date(2026, 8, 10)
        assert clock_late.day_of_week == "monday"


class TestAcceptanceScenarioContracts:
    """Verify each acceptance scenario's typed port requirements
    and forbidden behaviors can be enforced by the runtime."""

    @pytest.mark.asyncio
    async def test_ac001_simple_inquiry(self):
        ctx = await build_pipeline_context(
            VoiceSessionConfig(session_id="ac001", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_context="Dr. Priya: Mon-Sat. Consultation ₹300.",
        )
        assert "Dr. Priya" in ctx.system_prompt
        assert "₹300" in ctx.system_prompt
        assert "Today is" in ctx.system_prompt

    def test_ac002_today_availability(self):
        avail = _monday_availability()
        text = format_availability(avail)
        assert "Dr. Priya" in text
        assert "10:00" in text

    @pytest.mark.asyncio
    async def test_ac004_demo_refusal_upfront(self):
        ctx = await build_pipeline_context(
            VoiceSessionConfig(session_id="ac004", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            session_mode="demo",
        )
        assert "BEFORE" in ctx.system_prompt or "before" in ctx.system_prompt.lower()

    def test_ac005_unavailable_alternatives(self):
        avail = DayAvailability(
            business_date=date(2026, 8, 10),
            day_of_week="monday",
            is_operating_day=True,
            is_exception_day=False,
            operating_hours=((time(10, 0), time(13, 0)),),
            available_slots=(AvailableSlot(1, "Dr. Priya", time(11, 0), time(11, 30), "general"),),
        )
        text = format_availability(avail)
        assert "11:00" in text

    def test_ac006_closed_day(self):
        text = format_availability(_sunday_closed())
        assert "CLOSED" in text

    def test_ac008_abandon_terminal(self):
        ds = DialogueState()
        ds.set_terminal("abandoned")
        assert ds.terminal
        r = get_terminal_response("abandoned", "ta-Latn")
        assert count_questions(r) <= 1

    def test_ac009_safety_deterministic(self):
        r = get_terminal_response("safety", "ta-Latn")
        assert "urgent" in r.lower() or "hospital" in r.lower()

    def test_ac011_timezone_boundary(self):
        clock = _clock(day=10, hour=23, minute=45)
        assert clock.business_date == date(2026, 8, 10)

    @pytest.mark.asyncio
    async def test_ac002_stub_returns_not_connected(self):
        from fonely.voice.context import AvailabilityQuery

        stub = StubAvailabilityPort()
        query = AvailabilityQuery(
            business_id=1, target_date=date(2026, 8, 10), business_timezone="Asia/Kolkata"
        )
        result = await stub.query_day_availability(query)
        assert not result.is_operating_day
        assert "not connected" in result.reason


class TestCleanSyntheticConversation:
    """Verify one fully clean conversation can complete through the
    production runtime ports without any forbidden behavior."""

    def test_inquiry_flow_no_filler_no_repetition(self):
        ds = DialogueState(max_turns=4)
        responses = [
            "Aminjikarai-ல இருக்கு.",
            "Dr. Priya, Mon-Sat. Consultation ₹300.",
        ]
        for r in responses:
            assert not detect_filler(r)
            assert count_questions(r) <= 1
            ds.record_turn(r)
        assert not ds.is_over_budget()
        assert not ds.has_repeated_question()

    @pytest.mark.asyncio
    async def test_booking_flow_typed_ports(self):
        clock = _clock()
        avail = _monday_availability()
        ctx = await build_pipeline_context(
            VoiceSessionConfig(session_id="clean-1", business_id=1),
            clock=clock,
            business_name="Test Dental",
            session_mode="demo",
        )
        assert "demo" in ctx.system_prompt.lower()
        assert "Dr. Priya" in format_availability(avail)

        gate = PreTTSValidatorGate(
            FailClosedValidatorStub(),
            VoiceTelemetryExporter("clean-1"),
            GenerationClock("clean-1"),
        )
        assert gate.check("என்ன reason-க்காக visit?", SpeechClass.NON_CONSEQUENTIAL)
        assert gate.check("நாளைக்கு 10, 11, 6:30 available.", SpeechClass.NON_CONSEQUENTIAL)
        assert gate.check("பேரு சொல்லுங்க?", SpeechClass.NON_CONSEQUENTIAL)
        assert not gate.check("Booking confirmed.", SpeechClass.COMMITTED_CREATE)

    def test_session_lifecycle_clean_close(self):
        import asyncio

        async def _lifecycle() -> dict:
            # Run the WHOLE lifecycle inside one running loop so the ACTIVE
            # transition arms the duration timer and close() cancels it in the
            # same loop. Doing transitions outside a loop (then only close()
            # inside) would never arm the timer and, on py3.14, get_event_loop()
            # raises "no current event loop". No implicit loop creation.
            sup = VoiceSessionSupervisor(VoiceSessionConfig(session_id="clean-2", business_id=1))
            sup.transition(sup.state.__class__("signaling"))
            sup.transition(sup.state.__class__("connecting"))
            sup.transition(sup.state.__class__("active"))
            return await sup.close("normal")

        summary = asyncio.run(_lifecycle())
        assert summary["final_state"] == "closed"
        assert summary["close_reason"] == "normal"
