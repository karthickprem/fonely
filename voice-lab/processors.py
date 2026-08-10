"""Fonely-specific Pipecat processors."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from dialogue import BookingGoalTracker, classify_dialogue_act
from safety import classify
from style_retriever import ChennaiStyleRetriever


def message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def latest_user_text(messages) -> tuple[int | None, str]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            return index, message_text(message)
    return None, ""


def previous_assistant_text(messages, before_index: int) -> str:
    for index in range(before_index - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "assistant":
            return message_text(message)
    return ""


class TurnContextProcessor(FrameProcessor):
    """Build immutable turn-local dialogue and style context from raw caller text."""

    def __init__(self, retriever: ChennaiStyleRetriever, limit: int = 2):
        super().__init__()
        self._retriever = retriever
        self._limit = limit
        self._booking_goal = BookingGoalTracker()

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

        prior_assistant = previous_assistant_text(messages, index)
        state = classify_dialogue_act(actual_text, prior_assistant)
        booking_goal = self._booking_goal.update(actual_text, prior_assistant)
        examples = self._retriever.retrieve(actual_text, limit=self._limit)
        style_context = self._retriever.render(examples, actual_text)
        messages[index] = {
            "role": "user",
            "content": f"{state.render()}\n\n{booking_goal.render()}\n\n{style_context}",
        }
        request_context = LLMContext(
            messages=messages,
            tools=frame.context.tools,
            tool_choice=frame.context.tool_choice,
        )
        await self.push_frame(LLMContextFrame(context=request_context), direction)


BOOKING_SUCCESS = re.compile(
    r"(?:book(?:ing)?|appointment).*(?:confirm|confirmed|booked|saved|ஆயிடுச்சு|உறுதி)",
    re.IGNORECASE,
)
BOOKING_FAILURE_RESPONSE = (
    "Booking இன்னும் confirm ஆகல. Clinic staff கிட்ட verify பண்ணிக்கோங்க."
)


class ReceiptAwareTTSGate(FrameProcessor):
    """Block booking-success speech unless application commit evidence exists.

    ``receipt_provider`` is an injected application adapter callback. It may
    return a mapping or object carrying immutable committed evidence, or None.
    The voice processor never resolves availability, proposes, or commits.
    """

    def __init__(
        self,
        receipt_provider: Callable[[], Any] | None = None,
        *,
        business_id: int | None = None,
        business_timezone: str = "Asia/Kolkata",
        max_receipt_age_seconds: int = 120,
    ) -> None:
        super().__init__()
        self._receipt_provider = receipt_provider
        self._business_id = business_id
        self._timezone = ZoneInfo(business_timezone)
        self._max_receipt_age_seconds = max_receipt_age_seconds
        self._response_frames: list[Frame] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._response_frames = [frame]
            return
        if self._response_frames is None:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMTextFrame):
            self._response_frames.append(frame)
            return
        if not isinstance(frame, LLMFullResponseEndFrame):
            await self.push_frame(frame, direction)
            return

        buffered = self._response_frames
        self._response_frames = None
        text = "".join(
            candidate.text for candidate in buffered if isinstance(candidate, LLMTextFrame)
        )
        if not BOOKING_SUCCESS.search(text):
            for candidate in [*buffered, frame]:
                await self.push_frame(candidate, direction)
            return

        receipt = await self._get_receipt()
        if not self._valid_receipt(receipt):
            await self.push_frame(LLMFullResponseStartFrame(), direction)
            await self.push_frame(LLMTextFrame(text=BOOKING_FAILURE_RESPONSE), direction)
            await self.push_frame(LLMFullResponseEndFrame(), direction)
            return

        await self.push_frame(LLMFullResponseStartFrame(), direction)
        await self.push_frame(
            LLMTextFrame(text=self._format_receipt_confirmation(receipt)), direction
        )
        await self.push_frame(LLMFullResponseEndFrame(), direction)

    async def _get_receipt(self) -> Any:
        if self._receipt_provider is None:
            return None
        try:
            receipt = self._receipt_provider()
            if inspect.isawaitable(receipt):
                receipt = await receipt
            return receipt
        except (TimeoutError, asyncio.TimeoutError):
            return None
        except Exception:
            return None

    def _value(self, receipt: Any, name: str) -> Any:
        if isinstance(receipt, Mapping):
            return receipt.get(name)
        return getattr(receipt, name, None)

    def _valid_receipt(self, receipt: Any) -> bool:
        if receipt is None:
            return False
        if self._value(receipt, "status") != "committed":
            return False
        if self._value(receipt, "source") != "booking_application":
            return False
        if self._business_id is not None and self._value(receipt, "business_id") != self._business_id:
            return False
        if not isinstance(self._value(receipt, "appointment_id"), int):
            return False
        if not isinstance(self._value(receipt, "proposal_id"), int):
            return False
        if not isinstance(self._value(receipt, "proposal_version"), int):
            return False
        if not self._value(receipt, "confirmation_id"):
            return False
        if not self._value(receipt, "payload_digest"):
            return False
        committed_at = self._value(receipt, "committed_at")
        if not isinstance(committed_at, datetime) or committed_at.tzinfo is None:
            return False
        age = abs((datetime.now(committed_at.tzinfo) - committed_at).total_seconds())
        if age > self._max_receipt_age_seconds:
            return False
        start_at = self._value(receipt, "start_at_utc")
        end_at = self._value(receipt, "end_at_utc")
        if not isinstance(start_at, datetime) or not isinstance(end_at, datetime):
            return False
        if start_at.tzinfo is None or end_at.tzinfo is None or end_at <= start_at:
            return False
        return bool(
            self._value(receipt, "service_name")
            and self._value(receipt, "resource_name")
            and self._value(receipt, "business_timezone")
        )

    def _format_receipt_confirmation(self, receipt: Any) -> str:
        timezone = ZoneInfo(self._value(receipt, "business_timezone"))
        local_start = self._value(receipt, "start_at_utc").astimezone(timezone)
        local_date = local_start.strftime("%d %B %Y")
        local_time = local_start.strftime("%I:%M %p").lstrip("0")
        return (
            f"{self._value(receipt, 'service_name')} appointment, "
            f"{self._value(receipt, 'resource_name')} கிட்ட "
            f"{local_date} {local_time} confirm ஆயிடுச்சு."
        )


class DentalSafetyProcessor(FrameProcessor):
    """Bypass the LLM for deterministic urgent and medical responses.

    When safety triggers, the booking tracker still receives the caller text
    for state-only updates (e.g. abandonment) before the safety response is
    emitted. This prevents a caller who says "Stop, what medicine should I take?"
    from having their booking silently preserved.
    """

    def __init__(self, booking_tracker: BookingGoalTracker | None = None):
        super().__init__()
        self._booking_tracker = booking_tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        messages = frame.context.messages
        index, text = latest_user_text(messages)
        verdict = classify(text)
        if verdict is None:
            await self.push_frame(frame, direction)
            return

        if self._booking_tracker is not None:
            prior = previous_assistant_text(messages, index) if index is not None else ""
            self._booking_tracker.update(text, prior)

        has_tamil = any("஀" <= char <= "௿" for char in text)
        response = verdict["response_ta"] if has_tamil else verdict["response_en"]
        await self.push_frame(LLMFullResponseStartFrame(), direction)
        await self.push_frame(LLMTextFrame(text=response), direction)
        await self.push_frame(LLMFullResponseEndFrame(), direction)
