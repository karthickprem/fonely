"""Tests for pipeline builder, pre-TTS validator gate, and post-TTS generation gate."""

from datetime import UTC, date, datetime

import pytest

from fonely.voice.config import SpeechClass, VoiceSessionConfig
from fonely.voice.context import TrustedClock
from fonely.voice.generation import GenerationClock
from fonely.voice.pipeline import (
    PostTTSGenerationGate,
    PreTTSValidatorGate,
    build_pipeline_context,
)
from fonely.voice.telemetry import VoiceTelemetryExporter
from fonely.voice.validator_port import FailClosedValidatorStub


def _clock():
    return TrustedClock(
        now_utc=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, 10),
        day_of_week="monday",
    )


def _config():
    return VoiceSessionConfig(session_id="test-1", business_id=1)


class TestPipelineContext:
    @pytest.mark.asyncio
    async def test_build_with_required_args(self):
        ctx = await build_pipeline_context(_config(), clock=_clock(), business_name="Test Dental")
        assert "August 10, 2026" in ctx.system_prompt
        assert "Monday" in ctx.system_prompt
        assert "Tomorrow: 10, 11, 5, 6:30, 7:30" not in ctx.system_prompt
        assert ctx.session_mode == "demo"

    @pytest.mark.asyncio
    async def test_demo_mode_in_prompt(self):
        ctx = await build_pipeline_context(
            _config(), clock=_clock(), business_name="Test", session_mode="demo"
        )
        assert "demo" in ctx.system_prompt.lower()
        assert "cannot" in ctx.system_prompt.lower() or "CANNOT" in ctx.system_prompt

    @pytest.mark.asyncio
    async def test_business_name_in_greeting(self):
        ctx = await build_pipeline_context(_config(), clock=_clock(), business_name="Test Dental")
        assert "Test Dental" in ctx.greeting

    @pytest.mark.asyncio
    async def test_context_immutable(self):
        ctx = await build_pipeline_context(_config(), clock=_clock(), business_name="Test")
        try:
            ctx.session_mode = "live"
            raise AssertionError("should be frozen")
        except AttributeError:
            pass


class TestPreTTSValidatorGate:
    def test_allows_non_consequential(self):
        gate = PreTTSValidatorGate(
            FailClosedValidatorStub(),
            VoiceTelemetryExporter("test"),
            GenerationClock("test"),
        )
        assert gate.check("What date?", SpeechClass.NON_CONSEQUENTIAL)

    def test_blocks_committed(self):
        gate = PreTTSValidatorGate(
            FailClosedValidatorStub(),
            VoiceTelemetryExporter("test"),
            GenerationClock("test"),
        )
        assert not gate.check("Booking confirmed.", SpeechClass.COMMITTED_CREATE)

    def test_blocks_notification(self):
        gate = PreTTSValidatorGate(
            FailClosedValidatorStub(),
            VoiceTelemetryExporter("test"),
            GenerationClock("test"),
        )
        assert not gate.check("Doctor notified.", SpeechClass.NOTIFICATION_SENT)

    def test_emits_telemetry_on_block(self):
        tel = VoiceTelemetryExporter("test")
        gate = PreTTSValidatorGate(
            FailClosedValidatorStub(),
            tel,
            GenerationClock("test"),
        )
        gate.check("Booked.", SpeechClass.COMMITTED_CREATE)
        events = tel.drain()
        names = [e.name for e in events]
        assert "pre_tts_gate" in names
        assert "pre_tts_blocked" in names


class TestPostTTSGenerationGate:
    def test_allows_current_generation(self):
        clock = GenerationClock("test")
        clock.next_turn()
        gate = PostTTSGenerationGate(clock, VoiceTelemetryExporter("test"))
        assert gate.should_emit(clock.current().generation_id)
        assert gate.dropped_count == 0

    def test_drops_stale_generation(self):
        clock = GenerationClock("test")
        clock.next_turn()
        stale_gen = clock.current().generation_id
        clock.advance_generation()
        gate = PostTTSGenerationGate(clock, VoiceTelemetryExporter("test"))
        assert not gate.should_emit(stale_gen)
        assert gate.dropped_count == 1

    def test_emits_telemetry_on_drop(self):
        clock = GenerationClock("test")
        clock.next_turn()
        stale = clock.current().generation_id
        clock.advance_generation()
        tel = VoiceTelemetryExporter("test")
        gate = PostTTSGenerationGate(clock, tel)
        gate.should_emit(stale)
        events = tel.drain()
        assert any(e.name == "post_tts_dropped" for e in events)
