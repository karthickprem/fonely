"""Focused tests for the Pipecat voice-lab processors."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB_DIR))

from pipecat.frames.frames import (
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import run_test

from pipeline import clean_spoken_text
from dialogue import BookingGoalTracker, classify_dialogue_act
from processors import (
    BOOKING_FAILURE_RESPONSE,
    DentalSafetyProcessor,
    ReceiptAwareTTSGate,
    TurnContextProcessor,
)
from safety import classify
from style_retriever import ChennaiStyleRetriever

STYLE_CORPUS = LAB_DIR / "data" / "chennai_dental_style.json"


def test_spoken_text_cleanup_is_narrow():
    assert asyncio.run(clean_spoken_text("சசரிங்க, நாளைக்கு வரீங்களா?", None)) == "சரிங்க, நாளைக்கு வரீங்களா?"
    assert asyncio.run(clean_spoken_text("அய்யோ, ரொம்ப வலிக்குதா?", None)) == "அய்யோ, ரொம்ப வலிக்குதா?"
    assert asyncio.run(clean_spoken_text("slots available-ஆ இருக்கு", None)) == "slots available இருக்கு"
    assert asyncio.run(clean_spoken_text("6:30 slot available", None)) == "6:30 slot available"


def test_style_corpus_contains_no_operational_claims_or_placeholders():
    import json

    payload = json.loads(STYLE_CORPUS.read_text())
    serialized = json.dumps(payload["examples"], ensure_ascii=False).casefold()
    for forbidden in [
        "action:",
        "action result:",
        "book aayiduchu",
        "confirm aayiduchu",
        "confirmation message",
        "doctor-kittayum alert",
        "{slot}",
        "{clinic_name}",
    ]:
        assert forbidden not in serialized


def test_tooth_pain_uses_reviewed_empathy_pattern():
    retriever = ChennaiStyleRetriever(STYLE_CORPUS)
    examples = retriever.retrieve("எனக்கு கொஞ்சம் பல் வலிக்குது", limit=3)
    assert examples[0]["id"] == "curated-pain"
    assert examples[0]["agent_tts"] == "அய்யோ, கஷ்டமா இருக்கும் ங்க. எவ்வளவு நாளா வலிக்குது?"
    assert "நல்ல வலி" not in retriever.render(examples, "எனக்கு கொஞ்சம் பல் வலிக்குது")


def test_style_retrieval_matches_booking_and_is_bounded():
    retriever = ChennaiStyleRetriever(STYLE_CORPUS)
    examples = retriever.retrieve("நாளைக்கு appointment வேணும்", limit=3)
    assert 1 <= len(examples) <= 3
    assert any("booking" in example["intents"] for example in examples)
    rendered = retriever.render(examples, "நாளைக்கு appointment வேணும்")
    assert "<chennai_style_references>" in rendered
    assert "Actual caller: நாளைக்கு appointment வேணும்" in rendered
    assert "ACTION:" not in rendered


def test_turn_context_does_not_mutate_conversation_history():
    async def run():
        context = LLMContext(
            messages=[{"role": "user", "content": "நாளைக்கு appointment வேணும்"}]
        )
        original = list(context.messages)
        down, _ = await run_test(
            TurnContextProcessor(ChennaiStyleRetriever(STYLE_CORPUS)),
            frames_to_send=[LLMContextFrame(context=context)],
        )
        styled = down[0]
        assert isinstance(styled, LLMContextFrame)
        assert context.messages == original
        assert "<dialogue_state>" in styled.context.messages[-1]["content"]
        assert "<chennai_style_references>" in styled.context.messages[-1]["content"]

    asyncio.run(run())


def test_dialogue_state_stops_irrelevant_booking_flow():
    assert classify_dialogue_act("Timing வேண்டாம், ஒரு question இருக்கு").must_not_offer_slot
    assert classify_dialogue_act("நீங்க புரிஞ்சுக்கல").latest_user_act == "repair"
    assert classify_dialogue_act("Appointment book பண்ண procedure என்ன?").latest_user_act == "booking_procedure"
    booking = classify_dialogue_act("நாளைக்கு appointment வேணும்")
    assert booking.booking_flow_active and not booking.must_not_offer_slot


def test_style_retrieval_is_intent_aware_for_education_and_repair():
    retriever = ChennaiStyleRetriever(STYLE_CORPUS)
    education = retriever.retrieve("What are the different types of teeth?", limit=2)
    repair = retriever.retrieve("நீங்க புரிஞ்சுக்கல, நான் வேற கேக்குறேன்", limit=2)
    assert education and all("education" in item["intents"] for item in education)
    assert repair and all("repair" in item["intents"] for item in repair)
    assert all("booking" not in item["intents"] for item in [*education, *repair])


def test_turn_context_uses_raw_caller_text_for_style_retrieval():
    class RecordingRetriever(ChennaiStyleRetriever):
        def __init__(self):
            self.received = None

        def retrieve(self, text: str, limit: int = 3):
            self.received = text
            return []

    async def run():
        retriever = RecordingRetriever()
        context = LLMContext(messages=[{"role": "user", "content": "Timing வேண்டாம்"}])
        original = list(context.messages)
        down, _ = await run_test(
            TurnContextProcessor(retriever),
            frames_to_send=[LLMContextFrame(context=context)],
        )
        assert context.messages == original
        assert retriever.received == "Timing வேண்டாம்"
        assert "must_not_offer_slot: true" in down[0].context.messages[-1]["content"]

    asyncio.run(run())


def test_slot_selection_continues_previous_slot_offer():
    prior = "நாளைக்கு 10, 11, 5, 6:30 slots available. எந்த time convenient?"
    for selection in [
        "6:30 works",
        "5 slot வேணும்",
        "evening please",
        "10 மணி போதும்",
        "5 pm please",
        "10:00 works",
    ]:
        state = classify_dialogue_act(selection, prior)
        assert state.latest_user_act == "slot_selection"
        assert state.booking_flow_active
        assert not state.must_not_offer_slot

    unrelated = classify_dialogue_act("10 நாளா வலிக்குது", prior)
    assert unrelated.latest_user_act != "slot_selection"
    pain_answer = classify_dialogue_act("10", "What time did the pain start?")
    assert pain_answer.latest_user_act != "slot_selection"


def test_turn_context_preserves_slot_selection_state():
    async def run():
        context = LLMContext(
            messages=[
                {
                    "role": "assistant",
                    "content": "நாளைக்கு 10, 11, 5, 6:30 slots available. எந்த time convenient?",
                },
                {"role": "user", "content": "6:30 works"},
            ]
        )
        down, _ = await run_test(
            TurnContextProcessor(ChennaiStyleRetriever(STYLE_CORPUS)),
            frames_to_send=[LLMContextFrame(context=context)],
        )
        content = down[0].context.messages[-1]["content"]
        assert "latest_user_act: slot_selection" in content
        assert "must_not_offer_slot: false" in content
        assert "timing விடுங்க" not in content

    asyncio.run(run())


def test_pipeline_has_no_canned_relevance_rewrite():
    import inspect
    import processors

    source = inspect.getsource(processors)
    assert "சரிங்க, timing விடுங்க" not in source
    assert "same answer repeat ஆயிடுச்சு" not in source


def test_booking_goal_survives_tangent_and_advances_one_field():
    goal = BookingGoalTracker()
    started = goal.update(
        "உங்க clinic எங்க இருக்கு? நான் appointment book பண்ணனும்.",
        "எப்படி help பண்ணலாம்?",
    )
    assert started.active
    assert started.required_field == "reason"

    reason = goal.update(
        "பல்லு வலிக்குது, cleaning பண்ணனும். Insurance claim முடியுமா?",
        "என்ன reason-க்காக visit பண்ணணும்?",
    )
    assert reason.reason is not None
    assert reason.required_field == "preferred_date"
    assert reason.tangent_count == 1

    thanked = goal.update("ஓகேங்க thanks", "Insurance staff கிட்ட check பண்ணுங்க.")
    assert thanked.active
    assert thanked.required_field == "preferred_date"


def test_booking_goal_recognizes_tamil_script_book_variants():
    for text in (
        "appointment புக் பண்ணனும்",
        "appointment புக் பண்ண வேண்டும்",
        "அப்பாயிண்ட்மெண்ட் புக் பண்ணனும்",
        "நான் appointment புக் பண்ண விரும்புறேன்",
    ):
        goal = BookingGoalTracker()
        started = goal.update(text, "எப்படி help பண்ணலாம்?")
        assert started.active, text
        assert started.required_field == "reason", text


def test_booking_pipeline_orders_collection_and_receipt_gate_around_llm():
    source = (LAB_DIR / "booking_pipeline.py").read_text()
    pipeline_start = source.index("pipeline = Pipeline([")
    pipeline_source = source[pipeline_start:]
    assert pipeline_source.index("turn_context") < pipeline_source.index("llm,")
    assert pipeline_source.index("llm,") < pipeline_source.index("receipt_gate")
    assert pipeline_source.index("receipt_gate") < pipeline_source.index("tts,")


def test_booking_goal_preserves_today_and_selected_offer_across_reason_turn():
    goal = BookingGoalTracker()

    requested = goal.update(
        "இன்னைக்கு எனக்கு 12 மணிக்கு appointment புக் பண்ணனும்.",
        "வணக்கம்! Smile Dental Clinic, Aminjikarai. என்ன help வேணும்?",
    )
    assert requested.active
    assert requested.preferred_date == "இன்னைக்கு"
    assert requested.preferred_time == "12 மணி"

    selected = goal.update(
        "எனக்கு 05:00 மணிக்கு ஓகே.",
        "இன்னைக்கு 12 மணிக்கு slot available இல்ல. 10, 11, 5, 6:30, 7:30 இருக்கு. எந்த time convenient?",
    )
    assert selected.preferred_date == "இன்னைக்கு"
    assert selected.preferred_time == "05:00 மணி"
    assert selected.required_field == "reason"

    reason = goal.update(
        "பல்லு வலிக்காக பல்லு சொத்தை. Chocolate சாப்டா.",
        "என்ன reason-க்காக visit பண்ணணும்?",
    )
    assert reason.preferred_date == "இன்னைக்கு"
    assert reason.preferred_time == "05:00 மணி"
    assert reason.required_field == "patient_name"


def test_booking_goal_date_change_invalidates_selected_time():
    goal = BookingGoalTracker()
    goal.update(
        "இன்னைக்கு appointment புக் பண்ணனும், scaling வேணும்",
        "எப்படி help பண்ணலாம்?",
    )
    goal.update(
        "5 pm",
        "இன்னைக்கு 5:00 slot available. எந்த time convenient?",
    )

    changed = goal.update("நாளைக்கு வேணும்", "பேரு சொல்லுங்க?")

    assert changed.preferred_date == "நாளைக்கு"
    assert changed.preferred_time is None
    assert changed.required_field == "preferred_time"


def test_booking_goal_reaches_bounded_demo_terminal_state():
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "எப்படி help பண்ணலாம்?")
    dated = goal.update("நாளைக்கு", "எந்த நாள் convenient?")
    assert dated.required_field == "preferred_time"
    timed = goal.update(
        "6:30 works",
        "10, 11, 5, 6:30 slots available. எந்த time convenient?",
    )
    assert timed.required_field == "patient_name"
    named = goal.update("Karthick", "உங்க பேரு சொல்லுங்க?")
    assert named.status == "awaiting_confirmation"
    assert named.required_field == "confirmation"
    done = goal.update("ஆம் confirm", "இந்த details correct-ஆ?")
    assert done.status == "demo_complete"
    assert done.required_field is None


def test_booking_goal_new_request_clears_previous_fields():
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "எப்படி help பண்ணலாம்?")
    goal.update("நாளைக்கு", "எந்த நாள் convenient?")
    goal.update("6:30 works", "6:30 slot available. எந்த time convenient?")
    goal.update("Karthick", "உங்க பேரு சொல்லுங்க?")
    goal.update("ஆம் confirm", "இந்த details correct-ஆ?")
    fresh = goal.update("வேற appointment book பண்ணனும், extraction", "Call முடிந்தது")
    assert fresh.status == "collecting"
    assert fresh.reason is not None
    assert fresh.preferred_date is None
    assert fresh.preferred_time is None
    assert fresh.patient_name is None


def test_booking_goal_correction_wins_over_affirmative_prefix():
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "எப்படி help பண்ணலாம்?")
    goal.update("நாளைக்கு", "எந்த நாள் convenient?")
    goal.update("6:30 works", "6:30 slot available. எந்த time convenient?")
    goal.update("Karthick", "உங்க பேரு சொல்லுங்க?")
    changed = goal.update("yes, change time", "இந்த details correct-ஆ?")
    assert changed.status == "collecting"
    assert changed.required_field == "preferred_time"


def test_booking_goal_does_not_treat_thanks_as_confirmation():
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "எப்படி help பண்ணலாம்?")
    goal.update("நாளைக்கு", "எந்த நாள் convenient?")
    goal.update("6:30 works", "6:30 slot available. எந்த time convenient?")
    ready = goal.update("Karthick", "உங்க பேரு சொல்லுங்க?")
    assert ready.status == "awaiting_confirmation"
    thanked = goal.update("okay thanks", "இந்த details correct-ஆ?")
    assert thanked.status == "awaiting_confirmation"
    assert thanked.required_field == "confirmation"


def test_booking_goal_reopens_corrected_field_after_readback():
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "எப்படி help பண்ணலாம்?")
    goal.update("நாளைக்கு", "எந்த நாள் convenient?")
    goal.update("6:30 works", "6:30 slot available. எந்த time convenient?")
    goal.update("Karthick", "உங்க பேரு சொல்லுங்க?")
    changed = goal.update("இல்ல time மாத்தணும்", "இந்த details correct-ஆ?")
    assert changed.status == "collecting"
    assert changed.required_field == "preferred_time"

    goal.update("5 pm", "5 slot available. எந்த time convenient?")
    goal.update("Karthick", "உங்க பேரு சொல்லுங்க?")
    corrected_name = goal.update("இல்ல name மாத்தணும்", "இந்த details correct-ஆ?")
    assert corrected_name.status == "collecting"
    assert corrected_name.required_field == "patient_name"


def test_booking_goal_recognizes_natural_tanglish_name_prompt():
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "எப்படி help பண்ணலாம்?")
    goal.update("நாளைக்கு", "எந்த date-ல வர prefer பண்றீங்க?")
    goal.update("6:30 works", "6:30 slot available. எந்த time convenient?")
    named = goal.update("Meena", "உங்க பேஷன்ட் நேம் சொல்லுங்க?")
    assert named.patient_name == "Meena"
    assert named.status == "awaiting_confirmation"


def test_booking_goal_recognizes_natural_tanglish_date_prompt():
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "எப்படி help பண்ணலாம்?")
    dated = goal.update("நாளைக்கு", "எந்த date-ல வர prefer பண்றீங்க?")
    assert dated.preferred_date == "நாளைக்கு"
    assert dated.required_field == "preferred_time"


def test_booking_goal_extracts_multi_fact_naturally():
    goal = BookingGoalTracker()
    goal.update("Scaling appointment பண்ணனும், tomorrow.", "எப்படி help பண்ணலாம்?")
    snap = goal.snapshot()
    assert snap.reason is not None
    assert snap.preferred_date is not None
    assert snap.required_field == "preferred_time"

    goal2 = BookingGoalTracker()
    goal2.update("appointment book பண்ணனும்", "எப்படி help பண்ணலாம்?")
    tangent = goal2.update("fee எவ்வளவு?", "உங்க பேரு சொல்லுங்க?")
    assert tangent.patient_name is None


def test_booking_goal_accepts_supported_time_without_slot_word_in_prompt():
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "எப்படி help பண்ணலாம்?")
    goal.update("நாளைக்கு", "எந்த date-ல வர prefer பண்றீங்க?")
    timed = goal.update("5 PM works", "10, 11, 5, 6:30, 7:30 — எந்த time மாத்தணும்?")
    assert timed.preferred_time == "5 PM"
    assert timed.required_field == "patient_name"


def test_booking_goal_question_answers_do_not_count_as_tangents():
    goal = BookingGoalTracker(max_tangents=0)
    goal.update("appointment book பண்ணனும்", "எப்படி help பண்ணலாம்?")
    reason = goal.update("Cleaning?", "என்ன reason-க்காக visit பண்ணணும்?")
    assert reason.tangent_count == 0
    date = goal.update("Tomorrow?", "Which date?")
    assert date.tangent_count == 0


def test_booking_goal_confirmation_wins_on_final_budget_turn():
    goal = BookingGoalTracker(max_turns=5)
    goal.update("appointment book பண்ணனும், scaling வேணும்", "எப்படி help பண்ணலாம்?")
    goal.update("நாளைக்கு", "எந்த நாள் convenient?")
    goal.update("6:30 works", "6:30 slot available. எந்த time convenient?")
    goal.update("Karthick", "உங்க பேரு சொல்லுங்க?")
    done = goal.update("ஆம் confirm", "இந்த details correct-ஆ?")
    assert done.status == "demo_complete"


def test_turn_context_injects_active_goal_after_insurance_tangent():
    async def run():
        processor = TurnContextProcessor(ChennaiStyleRetriever(STYLE_CORPUS))
        first = LLMContext(
            messages=[
                {"role": "user", "content": "appointment book பண்ணனும்"},
            ]
        )
        await run_test(processor, frames_to_send=[LLMContextFrame(context=first)])
        tangent = LLMContext(
            messages=[
                {"role": "user", "content": "appointment book பண்ணனும்"},
                {"role": "assistant", "content": "என்ன reason-க்காக visit பண்ணணும்?"},
                {
                    "role": "user",
                    "content": "பல்லு வலிக்குது, cleaning பண்ணனும். Insurance claim முடியுமா?",
                },
            ]
        )
        down, _ = await run_test(processor, frames_to_send=[LLMContextFrame(context=tangent)])
        content = down[0].context.messages[-1]["content"]
        assert "<booking_goal>" in content
        assert "status: collecting" in content
        assert "required_field: preferred_date" in content
        assert "tangent_count: 1" in content
        assert "Do not say goodbye" in content

    asyncio.run(run())


def test_booking_goal_is_bounded_to_handoff():
    goal = BookingGoalTracker(max_turns=3, max_tangents=1)
    goal.update("appointment book பண்ணனும்", "எப்படி help பண்ணலாம்?")
    goal.update("insurance claim முடியுமா?", "என்ன reason?")
    final = goal.update("fee எவ்வளவு?", "என்ன reason?")
    assert final.status == "handoff_required"
    assert final.required_field is None


def test_safety_classification():
    assert classify("heavy bleeding")['type'] == 'urgent'
    assert classify("என்ன மருந்து சாப்பிடலாம்?")['type'] == 'medical'
    assert classify("appointment நாளைக்கு வேணும்") is None
    assert classify("Pain tablet எத்தனை mg எடுக்கணும்?")['type'] == 'medical'
    assert classify("Should I get a root canal or extraction?")['type'] == 'medical'
    assert classify("How many tablets should I take?")['type'] == 'medical'
    assert classify("Scaling appointment book பண்ணனும்") is None
    assert classify("நாளைக்கு 6:30 slot வேணும்") is None
    assert classify("Should I come in the morning or evening?") is None


def test_safety_abandon_stop_medicine():
    from processors import (
    BOOKING_FAILURE_RESPONSE,
    DentalSafetyProcessor,
    ReceiptAwareTTSGate,
    TurnContextProcessor,
)
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "")
    goal.update("நாளைக்கு", "எந்த date-ல வரணும்?")
    assert goal.snapshot().status == "collecting"
    safety = DentalSafetyProcessor(booking_tracker=goal)
    async def run():
        context = LLMContext(messages=[
            {"role": "user", "content": "appointment book பண்ணனும்"},
            {"role": "assistant", "content": "எந்த date-ல வரணும்?"},
            {"role": "user", "content": "Stop, what medicine should I take?"},
        ])
        down, _ = await run_test(safety, frames_to_send=[LLMContextFrame(context=context)])
        assert any(isinstance(f, LLMTextFrame) for f in down)
        assert goal.snapshot().status == "abandoned"
    asyncio.run(run())


def test_confirmation_with_punctuation():
    from dialogue import _is_confirmation, _rejects_confirmation
    assert _is_confirmation("Yes.")
    assert _is_confirmation("ஆம்.")
    assert _is_confirmation("yes!")
    assert _is_confirmation("confirm,")
    assert not _is_confirmation("maybe yes")
    assert not _rejects_confirmation("I know the details are correct")
    assert _rejects_confirmation("No, change time")
    assert _rejects_confirmation("wrong name")
    assert not _rejects_confirmation("I know it's okay")


def test_correction_clears_reason_and_dependents():
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "")
    goal.update("நாளைக்கு", "எந்த date-ல வரணும்?")
    goal.update("6:30 works", "10, 11, 5, 6:30, 7:30 available. எந்த time?")
    goal.update("Ravi", "உங்க பேரு சொல்லுங்க?")
    corrected = goal.update("No, reason is extraction", "Scaling, நாளைக்கு 6:30, Ravi. correct-ஆ?")
    assert corrected.status == "collecting"
    assert "extraction" in corrected.reason.casefold()
    assert corrected.preferred_date is None
    assert corrected.preferred_time is None
    assert corrected.patient_name is None

    goal2 = BookingGoalTracker()
    goal2.update("appointment book பண்ணனும், scaling வேணும்", "")
    goal2.update("நாளைக்கு", "எந்த date-ல வரணும்?")
    goal2.update("6:30 works", "10, 11, 5, 6:30, 7:30 available. எந்த time?")
    goal2.update("Ravi", "உங்க பேரு சொல்லுங்க?")
    corrected2 = goal2.update("No, wrong reason", "Scaling, நாளைக்கு 6:30, Ravi. correct-ஆ?")
    assert corrected2.status == "collecting"
    assert corrected2.reason is None
    assert corrected2.required_field == "reason"


def test_explicit_date_extraction():
    from dialogue import _extract_date
    assert _extract_date("12 August") is not None
    assert _extract_date("August 15") is not None
    assert _extract_date("15th August") is not None
    assert _extract_date("3rd January") is not None
    assert _extract_date("tomorrow") is not None
    assert _extract_date("12 August 2026") is not None
    assert _extract_date("scaling வேணும்") is None
    assert _extract_date("6:30 works") is None
    assert _extract_date("31 February") is None
    assert _extract_date("31 April") is None
    assert _extract_date("29 February 2025") is None
    assert _extract_date("29 February 2024") is not None
    assert _extract_date("my number is 9876543210") is None


def test_confirmation_natural_phrases():
    from dialogue import _is_confirmation, _rejects_confirmation
    assert _is_confirmation("Yes, that's correct")
    assert _is_confirmation("ஆம், சரி")
    assert _is_confirmation("correct-ஆ")
    assert not _is_confirmation("maybe")
    assert not _is_confirmation("yes, change time")


def test_name_correction_preserves_fields():
    goal = BookingGoalTracker()
    goal.update("appointment book பண்ணனும், scaling வேணும்", "")
    goal.update("நாளைக்கு", "எந்த date-ல வரணும்?")
    goal.update("6:30 works", "10, 11, 5, 6:30, 7:30 available. எந்த time?")
    goal.update("Ravi", "உங்க பேரு சொல்லுங்க?")
    corrected = goal.update("No, name is Meena", "Scaling, நாளைக்கு 6:30, Ravi. correct-ஆ?")
    assert corrected.patient_name == "Meena"
    assert corrected.preferred_date is not None
    assert corrected.preferred_time is not None
    assert corrected.reason is not None


def test_safety_treatment_choice_vs_booking():
    assert classify("Should I get a root canal or extraction?")['type'] == 'medical'
    assert classify("Should I get scaling done?") is None
    assert classify("Should I come in the morning or evening?") is None
    assert classify("I need a scaling appointment") is None
    assert classify("Should I choose extraction or filling?")['type'] == 'medical'


def test_booking_intent_bounded():
    from dialogue import _contains_booking_request
    assert _contains_booking_request("i need a root canal appointment")
    assert _contains_booking_request("appointment book பண்ணனும்")
    assert not _contains_booking_request("i need a root canal price explanation")
    assert not _contains_booking_request("i need a root canal")
    assert not _contains_booking_request("what is a root canal")


def test_duplicate_frame_blocked():
    """Logical-turn dedupe is BLOCKED: no stable turn identity on LLMContextFrame.
    Frame.id only dedupes the exact same object, which Pipecat doesn't redeliver.
    Content dedupe risks suppressing intentional repeated utterances.
    This test documents the open item."""
    pass


def test_safety_processor_bypasses_llm():
    async def run():
        context = LLMContext(messages=[{"role": "user", "content": "heavy bleeding"}])
        down, _ = await run_test(
            DentalSafetyProcessor(),
            frames_to_send=[LLMContextFrame(context=context)],
        )
        assert [type(frame) for frame in down] == [
            LLMFullResponseStartFrame,
            LLMTextFrame,
            LLMFullResponseEndFrame,
        ]
        assert "immediate medical care" in down[1].text

    asyncio.run(run())


def test_english_safety_response_is_english():
    async def run():
        context = LLMContext(messages=[{"role": "user", "content": "heavy bleeding"}])
        down, _ = await run_test(
            DentalSafetyProcessor(),
            frames_to_send=[LLMContextFrame(context=context)],
        )
        assert "immediate medical care" in down[1].text

    asyncio.run(run())


def test_structured_tamil_message_is_classified():
    async def run():
        context = LLMContext(
            messages=[{
                "role": "user",
                "content": [{"type": "text", "text": "என்ன மருந்து சாப்பிடலாம்?"}],
            }]
        )
        down, _ = await run_test(
            DentalSafetyProcessor(),
            frames_to_send=[LLMContextFrame(context=context)],
        )
        assert "medical advice" in down[1].text

    asyncio.run(run())


def test_safe_context_reaches_llm():
    async def run():
        frame = LLMContextFrame(
            context=LLMContext(messages=[{"role": "user", "content": "clinic எங்க இருக்கு?"}])
        )
        down, _ = await run_test(DentalSafetyProcessor(), frames_to_send=[frame])
        assert down == [frame]

    asyncio.run(run())
