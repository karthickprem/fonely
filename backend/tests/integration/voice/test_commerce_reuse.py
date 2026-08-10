"""Provider-free commerce/inventory scenario proving horizontal reuse.

The voice runtime's typed ports (TrustedClock, availability/inventory
query, validator gate, dialogue state) must work for commerce businesses
as easily as for dental appointments.  No clinic/patient/dentist
hardcoding in the core runtime path.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from fonely.voice.config import SpeechClass, VoiceSessionConfig
from fonely.voice.context import TrustedClock
from fonely.voice.dialogue import DialogueState, count_questions, detect_filler
from fonely.voice.generation import GenerationClock
from fonely.voice.mock_providers import (
    ConversationTurn,
    ScriptedConversation,
    run_scripted_conversation,
)
from fonely.voice.pipeline import PreTTSValidatorGate, build_pipeline_context
from fonely.voice.prompts import build_system_prompt
from fonely.voice.telemetry import VoiceTelemetryExporter
from fonely.voice.validator_port import FailClosedValidatorStub


def _clock():
    return TrustedClock(
        now_utc=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, 10),
        day_of_week="monday",
    )


class TestCommerceInquiry:
    """Prove the runtime supports a commerce inquiry without dental hardcoding."""

    def test_commerce_prompt_horizontal(self):
        prompt = build_system_prompt(
            clock=_clock(),
            clinic_name="Chennai Grocery Store",
            clinic_context="Products: Rice 5kg ₹350, Dal 1kg ₹120, Oil 1L ₹180. Delivery: 2-hour slot. Payment: cash, UPI.",
            availability=None,
            session_mode="live",
        )
        assert "Chennai Grocery Store" in prompt
        assert "Rice" in prompt or "₹350" in prompt
        assert "Monday" in prompt
        assert "Dr. Priya" not in prompt

    def test_commerce_order_inquiry_flow(self):
        script = ScriptedConversation(
            scenario_id="COMMERCE-001",
            expected_terminal="completed",
            expected_max_turns=6,
            turns=[
                ConversationTurn("Rice price enna?", "Rice 5kg ₹350."),
                ConversationTurn("2 pack order பண்ணனும்", "2 pack Rice, ₹700. Confirm?"),
                ConversationTurn("Yes", "Order noted. Delivery 2 hours-ல."),
            ],
        )
        result = run_scripted_conversation(script)
        assert result["pass"]
        assert result["filler_count"] == 0
        assert result["total_turns"] <= 6

    def test_commerce_inventory_check(self):
        script = ScriptedConversation(
            scenario_id="COMMERCE-002",
            expected_terminal="completed",
            expected_max_turns=4,
            turns=[
                ConversationTurn("Dal stock இருக்கா?", "Dal 1kg ₹120, stock available."),
                ConversationTurn("Thanks", "வணக்கம்."),
            ],
        )
        result = run_scripted_conversation(script)
        assert result["pass"]

    @pytest.mark.asyncio
    async def test_pipeline_context_horizontal(self):
        cfg = VoiceSessionConfig(session_id="commerce-ctx", business_id=42)
        ctx = await build_pipeline_context(
            cfg,
            clock=_clock(),
            business_name="Chennai Grocery Store",
            business_context="Products: Rice ₹350, Dal ₹120.",
            session_mode="live",
        )
        assert "Chennai Grocery Store" in ctx.system_prompt
        assert ctx.config.business_id == 42

    def test_validator_port_works_for_commerce(self):
        gate = PreTTSValidatorGate(
            FailClosedValidatorStub(),
            VoiceTelemetryExporter("commerce"),
            GenerationClock("commerce"),
        )
        assert gate.check("Rice 5kg ₹350.", SpeechClass.NON_CONSEQUENTIAL)
        assert not gate.check("Order confirmed and paid.", SpeechClass.COMMITTED_CREATE)

    def test_dialogue_state_horizontal(self):
        ds = DialogueState(max_turns=6)
        ds.record_turn("Rice 5kg ₹350.", asked_field="product")
        ds.record_turn("2 pack, ₹700. Confirm?", asked_field="confirmation")
        assert not ds.is_over_budget()
        assert not ds.has_repeated_question()
        assert not detect_filler("Rice 5kg ₹350.")
