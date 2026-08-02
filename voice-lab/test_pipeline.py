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

from processors import DentalSafetyProcessor
from safety import classify


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
