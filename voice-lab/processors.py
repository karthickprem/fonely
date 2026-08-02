"""Fonely-specific Pipecat processors."""

from __future__ import annotations

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from safety import classify


class DentalSafetyProcessor(FrameProcessor):
    """Bypass the LLM for deterministic urgent and medical responses."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        messages = frame.context.messages
        latest_user = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, dict) and message.get("role") == "user"
            ),
            None,
        )
        content = latest_user.get("content", "") if latest_user else ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            text = ""

        verdict = classify(text)
        if verdict is None:
            await self.push_frame(frame, direction)
            return

        has_tamil = any("஀" <= char <= "௿" for char in text)
        response = verdict["response_ta"] if has_tamil else verdict["response_en"]
        await self.push_frame(LLMFullResponseStartFrame(), direction)
        await self.push_frame(LLMTextFrame(text=response), direction)
        await self.push_frame(LLMFullResponseEndFrame(), direction)
