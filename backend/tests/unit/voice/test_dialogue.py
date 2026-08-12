"""Tests for dialogue state, turn budgets, repetition, and terminal responses."""

from fonely.voice.dialogue import (
    DialogueState,
    count_questions,
    detect_filler,
    get_terminal_response,
)


class TestDialogueState:
    def test_initial(self):
        ds = DialogueState()
        assert ds.turn_count == 0
        assert not ds.terminal
        assert not ds.is_over_budget()

    def test_turn_budget(self):
        ds = DialogueState(max_turns=3)
        ds.record_turn("response 1")
        ds.record_turn("response 2")
        assert not ds.is_over_budget()
        ds.record_turn("response 3")
        assert ds.is_over_budget()

    def test_repeated_question_detected(self):
        ds = DialogueState()
        ds.record_turn("What date?", asked_field="date")
        assert not ds.has_repeated_question()
        ds.record_turn("What date?", asked_field="date")
        assert ds.has_repeated_question()

    def test_different_field_no_repetition(self):
        ds = DialogueState()
        ds.record_turn("What date?", asked_field="date")
        ds.record_turn("What time?", asked_field="time")
        assert not ds.has_repeated_question()

    def test_terminal(self):
        ds = DialogueState()
        ds.set_terminal("abandoned")
        assert ds.terminal
        assert ds.terminal_reason == "abandoned"


class TestTerminalResponses:
    def test_abandoned_tanglish(self):
        r = get_terminal_response("abandoned", "ta-Latn")
        assert "booking" in r.lower() or "call" in r.lower()

    def test_abandoned_tamil(self):
        r = get_terminal_response("abandoned", "ta")
        assert "booking" in r

    def test_demo_complete(self):
        r = get_terminal_response("demo_complete", "en")
        assert "not saved" in r.lower()

    def test_safety(self):
        r = get_terminal_response("safety", "en")
        assert "urgent" in r.lower()

    def test_unknown_reason_defaults(self):
        r = get_terminal_response("unknown_reason", "en")
        assert len(r) > 0


class TestFillerDetection:
    def test_detects_english_filler(self):
        assert detect_filler("Sure, I can help you with that")
        assert detect_filler("Let me check the availability")
        assert detect_filler("I'll note that down")

    def test_detects_tamil_filler(self):
        assert detect_filler("சோ அதனால பாக்கணும்")

    def test_clean_response_no_filler(self):
        assert not detect_filler("நாளைக்கு 10, 6:30 available.")
        assert not detect_filler("What date works for you?")


class TestQuestionCount:
    def test_english_question(self):
        assert count_questions("What date?") == 1

    def test_multiple_questions(self):
        assert count_questions("What date? What time?") == 2

    def test_tamil_question_word(self):
        assert count_questions("என்ன service வேணும்") >= 1

    def test_no_question(self):
        assert count_questions("Tomorrow at 6:30.") == 0
