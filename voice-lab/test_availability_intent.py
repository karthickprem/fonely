"""Regression: availability-intent classifier for owner escalation (#43 part 1).

Two bugs the live demo surfaced, both fixed here:
  * OVER-BROAD KEYWORD: any sentence containing a time word ("நேரம்"/time) was
    treated as an availability question, so "எவ்வளவு நேரம் ஆகும்?" ("how long
    will it take?" — impatience/duration) would wrongly escalate to the owner.
  * OPERATOR PRECEDENCE: the inline condition was
    `"No confirmed availability" in ctx and A or B or C or ...` — since `and`
    binds tighter than `or`, the unconfirmed-day guard only gated the FIRST
    term, so a bare "slot"/"time" escalated on ANY day, even a confirmed one.

_is_availability_question isolates the intent; the escalation call site ANDs it
with the unconfirmed-day guard. These tests pin both.
"""

from __future__ import annotations

import ast
import os

from booking_pipeline import _is_availability_question

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.join(_THIS_DIR, "booking_pipeline.py")


class TestDurationQuestionsAreNotAvailability:
    def test_the_exact_demo_phrase_is_not_availability(self):
        # The phrase Karthick actually said in the live demo — "how long will
        # it take?" — must NOT read as an availability question.
        assert _is_availability_question("எவ்வளவு நேரம் ஆகும்?") is False

    def test_how_long_variants_not_availability(self):
        for phrase in (
            "how long will it take",
            "how much time",
            "எத்தன நேரம் ஆகும்",
            "evvalavu neram aagum",
        ):
            assert _is_availability_question(phrase) is False, phrase


class TestRealAvailabilityQuestionsEscalate:
    def test_slot_and_availability_words_are_availability(self):
        for phrase in (
            "என்ன slots இருக்கு?",
            "today availability iருக்கா?",
            "எந்த நேரம் free ஆ இருக்கு?",  # "which time is free" — genuine
            "any appointment available today?",
        ):
            assert _is_availability_question(phrase) is True, phrase

    def test_plain_time_question_without_duration_phrasing(self):
        # "what time is open" is availability; "how long" is not. The
        # discriminator is the duration phrasing, not the mere presence of a
        # time word.
        assert _is_availability_question("என்ன நேரம் இருக்கு?") is True


class TestNonAvailabilityChitchat:
    def test_pain_statement_is_not_availability(self):
        assert _is_availability_question("பல்லு வலிக்குது") is False

    def test_confirmation_is_not_availability(self):
        assert _is_availability_question("சரி ஓகே") is False


class TestPrecedenceBugIsGone:
    """The escalation site must AND the unconfirmed-day guard with the intent —
    not let a time word escalate on a confirmed day. Asserted structurally: the
    old buggy inline chain (a long `and ... or ... or ...`) must be gone and the
    call must go through _is_availability_question."""

    def test_call_site_uses_helper_and_guard_conjunction(self):
        with open(_PIPELINE) as f:
            src = f.read()
        # The helper is the intent gate at the escalation site.
        assert "_is_availability_question(user_text)" in src
        # The old over-broad inline disjunction must be gone.
        assert '"time" in user_text.lower() or' not in src
        assert '"நேரம்" in user_text.lower()' not in src

    def test_escalation_guarded_by_unconfirmed_day_ast(self):
        # Find the `if ... ask_doctor` guard and confirm it's a BoolOp(and) whose
        # operands include both the unconfirmed-day check and the helper call —
        # so neither alone triggers escalation (kills the precedence bug).
        with open(_PIPELINE) as f:
            tree = ast.parse(f.read())
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            # Does this If's body call BRIDGE.ask_doctor?
            calls_ask = any(
                isinstance(c, ast.Attribute) and c.attr == "ask_doctor"
                for c in ast.walk(node)
            )
            if not calls_ask:
                continue
            test = node.test
            assert isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And), (
                "escalation guard must be an AND of unconfirmed-day AND intent"
            )
            src_of_test = ast.dump(test)
            assert "No confirmed availability" in src_of_test
            assert "_is_availability_question" in src_of_test
            found = True
        assert found, "no ask_doctor escalation guard found"


if __name__ == "__main__":
    import sys

    mod = sys.modules[__name__]
    passed = 0
    for cls_name in dir(mod):
        cls = getattr(mod, cls_name)
        if isinstance(cls, type) and cls_name.startswith("Test"):
            inst = cls()
            for m in dir(inst):
                if m.startswith("test_"):
                    getattr(inst, m)()
                    passed += 1
    print(f"ALL {passed} availability-intent tests passed")
