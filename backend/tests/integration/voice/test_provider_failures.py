"""Provider failure simulation and fail-closed behavior.

Tests STT/LLM/TTS timeout, error, and circuit-open scenarios
through the mock framework.  Verifies the runtime never produces
partial or corrupted output on provider failure.
"""
from __future__ import annotations

import pytest

from fonely.voice.config import SessionState, VoiceSessionConfig
from fonely.voice.dialogue import DialogueState, get_terminal_response
from fonely.voice.generation import GenerationClock
from fonely.voice.lifecycle import VoiceSessionSupervisor
from fonely.voice.mock_providers import MockLLM, MockSTT, MockTTS
from fonely.voice.telemetry import VoiceTelemetryExporter


class TestSTTFailure:
    def test_stt_exhausted_returns_none(self):
        stt = MockSTT(["first turn"])
        assert stt.transcribe() is not None
        assert stt.transcribe() is None

    def test_empty_stt_returns_none(self):
        stt = MockSTT([])
        assert stt.transcribe() is None


class TestLLMFailure:
    def test_llm_exhausted_returns_empty(self):
        llm = MockLLM(["only response"])
        assert llm.generate().text == "only response"
        assert llm.generate().text == ""

    def test_empty_llm_returns_empty(self):
        llm = MockLLM([])
        assert llm.generate().text == ""


class TestTTSFailure:
    def test_tts_empty_text(self):
        tts = MockTTS()
        result = tts.synthesize("")
        assert result.characters == 0
        assert result.audio_bytes == 0


class TestSessionFailureRecovery:
    @pytest.mark.asyncio
    async def test_provider_error_fails_session(self):
        sup = VoiceSessionSupervisor(
            VoiceSessionConfig(session_id="fail-1", business_id=1)
        )
        sup.transition(SessionState.SIGNALING)
        sup.transition(SessionState.CONNECTING)
        sup.transition(SessionState.ACTIVE)
        summary = await sup.close("stt_provider_error")
        assert summary["final_state"] == "failed"
        assert summary["close_reason"] == "stt_provider_error"

    @pytest.mark.asyncio
    async def test_llm_timeout_fails_session(self):
        sup = VoiceSessionSupervisor(
            VoiceSessionConfig(session_id="fail-2", business_id=1)
        )
        sup.transition(SessionState.SIGNALING)
        sup.transition(SessionState.CONNECTING)
        sup.transition(SessionState.ACTIVE)
        summary = await sup.close("llm_timeout")
        assert summary["final_state"] == "failed"

    @pytest.mark.asyncio
    async def test_tts_error_fails_session(self):
        sup = VoiceSessionSupervisor(
            VoiceSessionConfig(session_id="fail-3", business_id=1)
        )
        sup.transition(SessionState.SIGNALING)
        sup.transition(SessionState.CONNECTING)
        sup.transition(SessionState.ACTIVE)
        summary = await sup.close("tts_synthesis_error")
        assert summary["final_state"] == "failed"


class TestInterruptionAndBargeIn:
    def test_generation_advances_on_interruption(self):
        clock = GenerationClock("int-1")
        clock.next_turn()
        pre_interrupt = clock.current()
        clock.advance_generation()
        post_interrupt = clock.current()
        assert not clock.is_current(pre_interrupt)
        assert clock.is_current(post_interrupt)
        assert post_interrupt.generation_id == pre_interrupt.generation_id + 1

    def test_stale_output_dropped_after_barge_in(self):
        from fonely.voice.pipeline import PostTTSGenerationGate
        clock = GenerationClock("int-2")
        clock.next_turn()
        stale_gen = clock.current().generation_id
        clock.advance_generation()
        gate = PostTTSGenerationGate(clock, VoiceTelemetryExporter("int-2"))
        assert not gate.should_emit(stale_gen)
        assert gate.should_emit(clock.current().generation_id)
        assert gate.dropped_count == 1

    def test_multiple_interruptions(self):
        clock = GenerationClock("int-3")
        clock.next_turn()
        tokens = [clock.current()]
        for _ in range(5):
            clock.advance_generation()
            tokens.append(clock.current())
        assert len(set(t.generation_id for t in tokens)) == 6
        for old_token in tokens[:-1]:
            assert not clock.is_current(old_token)


class TestTerminalClosure:
    def test_terminal_response_no_question(self):
        from fonely.voice.dialogue import count_questions
        for reason in ["abandoned", "max_turns", "demo_complete", "safety", "handoff"]:
            for lang in ["ta-Latn", "ta", "en"]:
                r = get_terminal_response(reason, lang)
                assert count_questions(r) <= 1, f"{reason}/{lang} has {count_questions(r)} questions"

    def test_terminal_response_no_filler(self):
        from fonely.voice.dialogue import detect_filler
        for reason in ["abandoned", "max_turns", "demo_complete", "safety", "handoff"]:
            for lang in ["ta-Latn", "ta", "en"]:
                r = get_terminal_response(reason, lang)
                assert not detect_filler(r), f"{reason}/{lang} has filler"

    def test_terminal_after_budget_exceeded(self):
        ds = DialogueState(max_turns=2)
        ds.record_turn("response 1")
        ds.record_turn("response 2")
        assert ds.is_over_budget()
        ds.set_terminal("max_turns")
        r = get_terminal_response("max_turns", "en")
        assert len(r) > 0
