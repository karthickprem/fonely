"""Full multi-turn mock STT→LLM→TTS conversations for AC-001..012.

Each scenario runs a complete scripted conversation through the
production runtime's typed ports and verifies turn budget, no filler,
no repeated questions, max one question per turn, correct terminal
outcome, and correct validator gate behavior.
"""

from __future__ import annotations

import json

import pytest

from fonely.voice.mock_providers import (
    ConversationTurn,
    ScriptedConversation,
    run_scripted_conversation,
)

AC001 = ScriptedConversation(
    scenario_id="AC-001",
    expected_terminal="completed",
    expected_max_turns=4,
    turns=[
        ConversationTurn("Clinic எங்க இருக்கு?", "Aminjikarai-ல இருக்கு."),
        ConversationTurn("Fee எவ்வளவு?", "Consultation ₹300."),
    ],
)

AC002 = ScriptedConversation(
    scenario_id="AC-002",
    expected_terminal="completed",
    expected_max_turns=4,
    turns=[
        ConversationTurn(
            "இன்னைக்கு doctor free-ஆ?", "இன்றைக்கு Dr. Priya 10:00, 11:00, 18:30 available."
        ),
        ConversationTurn("Thanks", "வணக்கம்."),
    ],
)

AC003 = ScriptedConversation(
    scenario_id="AC-003",
    expected_terminal="completed",
    expected_max_turns=10,
    turns=[
        ConversationTurn("Appointment book பண்ணனும்", "என்ன reason-க்காக visit?", asked_field="reason"),
        ConversationTurn("Scaling", "எந்த date-ல வரணும்?", asked_field="date"),
        ConversationTurn("நாளைக்கு", "18:30 available. இந்த time சரியா?", asked_field="time"),
        ConversationTurn("6:30 works", "பேரு சொல்லுங்க?", asked_field="name"),
        ConversationTurn("Karthick", "Scaling, நாளைக்கு 6:30, Karthick. Correct-ஆ?"),
        ConversationTurn(
            "ஆம்", "Details collect பண்ணிட்டேன், ஆனா save ஆகல. Clinic staff கிட்ட confirm பண்ணுங்க."
        ),
    ],
)

AC004 = ScriptedConversation(
    scenario_id="AC-004",
    expected_terminal="completed",
    expected_max_turns=3,
    turns=[
        ConversationTurn(
            "Appointment book பண்ணனும்", "இது demo — booking process show பண்ணலாம், ஆனா save ஆகாது."
        ),
    ],
)

AC005 = ScriptedConversation(
    scenario_id="AC-005",
    expected_terminal="completed",
    expected_max_turns=6,
    turns=[
        ConversationTurn(
            "நாளைக்கு 3 மணிக்கு slot இருக்கா?", "3:00 available இல்லை. 10:00, 11:00, 18:30 available."
        ),
        ConversationTurn("11 works", "11:00-க்கு note பண்ணிட்டேன்."),
    ],
)

AC006 = ScriptedConversation(
    scenario_id="AC-006",
    expected_terminal="completed",
    expected_max_turns=4,
    turns=[
        ConversationTurn("Sunday appointment வேணும்", "Sunday clinic closed."),
        ConversationTurn("Monday?", "Monday 10:00, 11:00, 18:30 available."),
    ],
)

AC007 = ScriptedConversation(
    scenario_id="AC-007",
    expected_terminal="completed",
    expected_max_turns=8,
    turns=[
        ConversationTurn("Appointment book பண்ணனும்", "என்ன reason-க்காக visit?", asked_field="reason"),
        ConversationTurn("Cleaning", "எந்த date-ல வரணும்?", asked_field="date"),
        ConversationTurn("நாளைக்கு", "18:30 available. Time சரியா?", asked_field="time"),
        ConversationTurn(
            "No, root canal-க்கு change பண்ணுங்க",
            "Root canal-க்கு மாத்திட்டேன். எந்த date?",
            asked_field="corrected_date",
        ),
        ConversationTurn("நாளைக்கே", "18:30 available. Time?", asked_field="corrected_time"),
        ConversationTurn("6:30", "பேரு சொல்லுங்க?", asked_field="name"),
    ],
)

AC008 = ScriptedConversation(
    scenario_id="AC-008",
    expected_terminal="completed",
    expected_max_turns=3,
    turns=[
        ConversationTurn("Appointment book பண்ணனும்", "என்ன reason-க்காக visit?", asked_field="reason"),
        ConversationTurn("விடுங்க, வேண்டாம்", "சரி, booking வேணும்னா call பண்ணுங்க."),
    ],
)

AC009 = ScriptedConversation(
    scenario_id="AC-009",
    expected_terminal="completed",
    expected_max_turns=2,
    turns=[
        ConversationTurn(
            "Heavy bleeding, can't stop",
            "இது urgent-ஆ இருக்கலாம். Hospital போங்க அல்லது emergency services call பண்ணுங்க.",
        ),
    ],
)

AC010 = ScriptedConversation(
    scenario_id="AC-010",
    expected_terminal="completed",
    expected_max_turns=4,
    turns=[
        ConversationTurn("Fee எவ்வளவு?", "Consultation ₹300."),
        ConversationTurn("Fee எவ்வளவு?", "Consultation ₹300."),
    ],
)

AC011 = ScriptedConversation(
    scenario_id="AC-011",
    expected_terminal="completed",
    expected_max_turns=4,
    turns=[
        ConversationTurn(
            "Today doctor free-ஆ?", "இன்றைக்கு Monday. Dr. Priya 10:00, 11:00, 18:30 available."
        ),
    ],
)

AC012 = ScriptedConversation(
    scenario_id="AC-012",
    expected_terminal="completed",
    expected_max_turns=4,
    turns=[
        ConversationTurn(
            "Insurance claim process explain பண்ணுங்க",
            "Insurance-க்கு clinic staff கிட்ட பேசுங்க. Automated help இதுக்கு முடியாது.",
        ),
    ],
)

ALL_SCENARIOS = [AC001, AC002, AC003, AC004, AC005, AC006, AC007, AC008, AC009, AC010, AC011, AC012]


class TestAcceptanceConversations:
    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS])
    def test_scenario_passes(self, scenario):
        result = run_scripted_conversation(scenario)
        assert result["pass"], (
            f"{scenario.scenario_id}: filler={result['filler_count']}, "
            f"multi_q={result['multi_question_count']}, "
            f"repeated={result['repeated_questions']}, "
            f"turns={result['total_turns']}/{result['max_turns']}"
        )

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS])
    def test_turn_budget_respected(self, scenario):
        result = run_scripted_conversation(scenario)
        assert result["total_turns"] <= scenario.expected_max_turns

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS])
    def test_no_filler(self, scenario):
        result = run_scripted_conversation(scenario)
        assert result["filler_count"] == 0

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS])
    def test_max_one_question_per_turn(self, scenario):
        result = run_scripted_conversation(scenario)
        assert result["multi_question_count"] == 0

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS])
    def test_usage_accounting(self, scenario):
        result = run_scripted_conversation(scenario)
        assert result["stt_seconds"] > 0
        assert result["llm_input_tokens"] > 0
        assert result["llm_output_tokens"] > 0
        if not any(t["speech_class"] != "non_consequential" for t in result["turns"]):
            assert result["tts_characters"] > 0


class TestConsequentialBlocking:
    def test_committed_speech_blocked(self):
        script = ScriptedConversation(
            scenario_id="BLOCK-001",
            expected_max_turns=3,
            turns=[
                ConversationTurn(
                    "Confirm the booking",
                    "Booking confirmed for tomorrow at 6:30.",
                    speech_class="committed_create",
                ),
            ],
        )
        result = run_scripted_conversation(script)
        assert result["blocked_count"] == 1
        assert result["tts_characters"] == 0

    def test_notification_blocked(self):
        script = ScriptedConversation(
            scenario_id="BLOCK-002",
            expected_max_turns=3,
            turns=[
                ConversationTurn(
                    "Did you notify the doctor?",
                    "Doctor has been notified.",
                    speech_class="notification_sent",
                ),
            ],
        )
        result = run_scripted_conversation(script)
        assert result["blocked_count"] == 1


class TestEvidencePersistence:
    def test_evidence_serializable(self):
        for scenario in ALL_SCENARIOS:
            result = run_scripted_conversation(scenario)
            serialized = json.dumps(result, ensure_ascii=False)
            parsed = json.loads(serialized)
            assert parsed["scenario_id"] == scenario.scenario_id
            assert "caller_text" not in serialized
            assert "expected_response" not in serialized
