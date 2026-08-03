"""Focused tests for the Pipecat voice-lab processors."""

import asyncio
import sys
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
from processors import ChennaiStyleProcessor, DentalSafetyProcessor
from safety import classify
from style_retriever import ChennaiStyleRetriever

STYLE_CORPUS = LAB_DIR / "data" / "chennai_dental_style.json"


def test_spoken_text_cleanup_is_narrow():
    assert asyncio.run(clean_spoken_text("சசரிங்க, நாளைக்கு வரீங்களா?", None)) == "சரிங்க, நாளைக்கு வரீங்களா?"
    assert asyncio.run(clean_spoken_text("அய்யோ, ரொம்ப வலிக்குதா?", None)) == "அய்யோ, ரொம்ப வலிக்குதா?"


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


def test_style_processor_does_not_mutate_conversation_history():
    async def run():
        context = LLMContext(
            messages=[{"role": "user", "content": "நாளைக்கு appointment வேணும்"}]
        )
        original = list(context.messages)
        down, _ = await run_test(
            ChennaiStyleProcessor(ChennaiStyleRetriever(STYLE_CORPUS)),
            frames_to_send=[LLMContextFrame(context=context)],
        )
        styled = down[0]
        assert isinstance(styled, LLMContextFrame)
        assert context.messages == original
        assert "<chennai_style_references>" in styled.context.messages[-1]["content"]

    asyncio.run(run())


def test_safety_classification():
    assert classify("heavy bleeding")['type'] == 'urgent'
    assert classify("என்ன மருந்து சாப்பிடலாம்?")['type'] == 'medical'
    assert classify("appointment நாளைக்கு வேணும்") is None


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
