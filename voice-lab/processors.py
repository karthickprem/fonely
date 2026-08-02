"""Fonely-specific Pipecat processors."""

from __future__ import annotations

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from safety import classify
from style_retriever import ChennaiStyleRetriever


def latest_user_text(messages) -> tuple[int | None, str]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return index, content
        if isinstance(content, list):
            text = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            return index, text
    return None, ""


class ChennaiStyleProcessor(FrameProcessor):
    """Inject turn-local style examples without mutating conversation history."""

    def __init__(self, retriever: ChennaiStyleRetriever, limit: int = 3):
        super().__init__()
        self._retriever = retriever
        self._limit = limit

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        messages = list(frame.context.messages)
        index, actual_text = latest_user_text(messages)
        if index is None or not actual_text:
            await self.push_frame(frame, direction)
            return

        examples = self._retriever.retrieve(actual_text, limit=self._limit)
        styled_text = self._retriever.render(examples, actual_text)
        messages[index] = {"role": "user", "content": styled_text}
        request_context = LLMContext(
            messages=messages,
            tools=frame.context.tools,
            tool_choice=frame.context.tool_choice,
        )
        await self.push_frame(LLMContextFrame(context=request_context), direction)


class DentalSafetyProcessor(FrameProcessor):
    """Bypass the LLM for deterministic urgent and medical responses."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        _, text = latest_user_text(frame.context.messages)
        verdict = classify(text)
        if verdict is None:
            await self.push_frame(frame, direction)
            return

        has_tamil = any("஀" <= char <= "௿" for char in text)
        response = verdict["response_ta"] if has_tamil else verdict["response_en"]
        await self.push_frame(LLMFullResponseStartFrame(), direction)
        await self.push_frame(LLMTextFrame(text=response), direction)
        await self.push_frame(LLMFullResponseEndFrame(), direction)
