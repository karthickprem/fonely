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

from dialogue import DialogueState, classify_dialogue_act, contains_false_confirmation, contains_unwanted_slot
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


class DialogueStateProcessor(FrameProcessor):
    """Inject a deterministic, non-authoritative latest-turn routing hint."""

    def __init__(self):
        super().__init__()
        self.current_state = DialogueState("unclear", False, True)

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
        self.current_state = classify_dialogue_act(actual_text)
        messages[index] = {
            "role": "user",
            "content": f"{self.current_state.render()}\n\nActual caller: {actual_text}",
        }
        await self.push_frame(
            LLMContextFrame(
                context=LLMContext(
                    messages=messages,
                    tools=frame.context.tools,
                    tool_choice=frame.context.tool_choice,
                )
            ),
            direction,
        )


class ChennaiStyleProcessor(FrameProcessor):
    """Inject turn-local style examples without mutating conversation history."""

    def __init__(self, retriever: ChennaiStyleRetriever, limit: int = 2):
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


class ResponseRelevanceProcessor(FrameProcessor):
    """Apply bounded relevance and repetition guards before TTS."""

    def __init__(self, dialogue_state: DialogueStateProcessor):
        super().__init__()
        self._dialogue_state = dialogue_state
        self._last_response = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMTextFrame):
            await self.push_frame(frame, direction)
            return
        text = frame.text.strip()
        state = self._dialogue_state.current_state
        normalized = " ".join(text.casefold().split())
        previous = " ".join(self._last_response.casefold().split())
        if contains_false_confirmation(text):
            text = "இது demo மட்டும்; actual booking save ஆகாது."
        elif state.must_not_offer_slot and contains_unwanted_slot(text):
            if state.latest_user_act == "general_question":
                text = "நீங்க கேட்ட question-க்கு direct-ஆ answer பண்றேன்; கொஞ்சம் clear-ஆ மறுபடி சொல்லுங்க?"
            elif state.latest_user_act == "repair":
                text = "Sorry, நான் தவறா புரிஞ்சுக்கிட்டேன். நீங்க கேட்டது மறுபடி சொல்லுங்க?"
            else:
                text = "சரிங்க, timing விடுங்க. நீங்க கேட்ட question என்ன சொல்லுங்க?"
        elif previous and normalized == previous:
            text = "Sorry, same answer repeat ஆயிடுச்சு. நீங்க இப்ப கேட்டது மறுபடி சொல்லுங்க?"
        self._last_response = text
        await self.push_frame(LLMTextFrame(text=text), direction)


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
