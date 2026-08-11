"""Pipecat FrameProcessors for the streaming voice booking pipeline.

This is the turn model that has carried real Tamil calls: frames flow through
Pipecat, the injector rewrites the LLM context pre-inference, the gate rewrites
the response post-inference. It replaces the blob-in/result-out runtime.py,
which could not barge-in.

Dependency-injected, not global-coupled: both processors take a ResolverContext
(business_id + session_factory + command_port) so they can be constructed for a
test, a demo, or a real call without importing a server module. Booking commits
go through clinic_resolver.book_appointment — the single commit path — never
directly here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .context import TrustedClock, resolve_relative_date
from .dialogue import BookingCollection, contains_medical_advice

logger = logging.getLogger("fonely.voice.frame_pipeline")

MEDICAL_SAFE_RESPONSE = (
    "அதற்கு doctor நேரில் பார்த்துதான் சொல்ல முடியும். Appointment book பண்ணலாமா?"
)
GOODBYE_RESPONSE = "நன்றி, take care! Clinic-ல சந்திப்போம்."
BOOKING_NOTED_RESPONSE = "Booking note பண்ணிட்டேன். வேற ஏதாவது doubt இருக்கா?"

_CONFIRM_WORDS = frozenset({
    "yes", "yeah", "yep", "ok", "okay", "correct", "right", "sure", "hmm",
    "ஆமா", "ஆம்", "சரி", "சரிங்க", "aamaa", "sari", "aama",
})
_CLOSURE_WORDS = ("no", "bye", "இல்ல", "போறேன்", "நன்றி", "thanks", "nothing", "வேண்டாம்")

# Words that signal the caller is asking about availability. Used to decide
# whether to ping the doctor when the schedule is unconfirmed.
_AVAILABILITY_WORDS = ("availability", "slot", "available", "time", "அவைலபிள", "நேரம்", "when")


def _is_confirmation(text: str) -> bool:
    return text.strip().casefold().rstrip(".!") in _CONFIRM_WORDS


@dataclass
class ResolverContext:
    """Everything the processors need to reach the DB and the commit port.

    Injected by the entrypoint. Holds no per-call mutable state itself.
    """
    business_id: int
    session_factory: Callable  # () -> AsyncSession context manager
    command_port: object       # CommandPort
    clock: TrustedClock
    ask_doctor: Callable | None = None  # async (question, patient_context) -> None


class BookingStateInjector(FrameProcessor):
    """Pre-LLM: rewrites the LLM context with deterministic booking state.

    The BookingCollection state machine owns which field is asked next; the LLM
    only renders it to natural speech. Live clinic context (real DB slots) is
    injected each turn so the model can never offer a slot the DB does not have.
    """

    def __init__(self, resolver: ResolverContext):
        super().__init__()
        self._resolver = resolver
        self._booking = BookingCollection()
        self._last_availability = None
        self.caller_confirmed = False
        self.booking_closed = False

    @property
    def booking(self) -> BookingCollection:
        return self._booking

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        messages = list(frame.context.messages)
        user_text = ""
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                user_text = content if isinstance(content, str) else ""
                break

        prev_assistant = ""
        for msg in reversed(messages[:-1]):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content", "")
                prev_assistant = content if isinstance(content, str) else ""
                break

        resolved_date = resolve_relative_date(user_text, self._resolver.clock)

        self._booking.update(
            user_text,
            resolved_date=resolved_date,
            availability=self._last_availability,
            previous_assistant_text=prev_assistant,
        )

        if self._booking.required_field == "confirmation" and _is_confirmation(user_text):
            self.caller_confirmed = True

        if self.caller_confirmed and not self.booking_closed:
            lower = user_text.strip().casefold()
            if any(w in lower for w in _CLOSURE_WORDS):
                self.booking_closed = True

        live_context = await self._build_live_context(user_text)

        state_block = self._booking.render()
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], dict) and messages[i].get("role") == "user":
                original = messages[i].get("content", "")
                messages[i] = {
                    "role": "user",
                    "content": f"{original}\n\n{live_context}\n{state_block}",
                }
                break

        new_context = LLMContext(
            messages=messages,
            tools=frame.context.tools,
            tool_choice=frame.context.tool_choice,
        )
        await self.push_frame(LLMContextFrame(context=new_context), direction)

    async def _build_live_context(self, user_text: str) -> str:
        """Fetch real clinic context from the DB and, if the schedule is
        unconfirmed AND the caller is asking about availability, ping the
        doctor. The precedence here is explicit — a bug in the lab version
        (`A and B or C or D`) grouped wrong and pinged on almost any input."""
        from . import clinic_resolver
        try:
            async with self._resolver.session_factory() as session:
                ctx_text = await clinic_resolver.clinic_context_text(
                    session, self._resolver.business_id
                )
        except Exception as exc:
            logger.warning("live_context_fetch_failed: %s", type(exc).__name__)
            return ""

        unconfirmed = "No confirmed availability" in ctx_text
        asking_availability = any(w in user_text.lower() for w in _AVAILABILITY_WORDS)
        if unconfirmed and asking_availability and self._resolver.ask_doctor is not None:
            try:
                await self._resolver.ask_doctor(
                    "Patient is asking about availability. What are your slots?",
                    user_text[:100],
                )
            except Exception as exc:
                logger.warning("ask_doctor_failed: %s", type(exc).__name__)

        return f"\n<live_clinic_context>\n{ctx_text}\n</live_clinic_context>\n"


class BookingPostLLMGate(FrameProcessor):
    """Post-LLM: deterministic gates on the model's output before TTS.

    Gate order (first match wins):
      1. Medical advice → safe doctor referral (never the model's medicine talk)
      2. Caller closed after confirmation → deterministic goodbye
      3. Caller confirmed → commit through the port, speak the receipt id
      4. All fields collected, model skipped readback → force the readback
      else pass the model's text through.

    The commit in gate 3 goes through clinic_resolver.book_appointment (the
    single commit path). This processor never constructs a booking itself.
    """

    def __init__(self, injector: BookingStateInjector, resolver: ResolverContext):
        super().__init__()
        self._injector = injector
        self._resolver = resolver
        self._booking_done = False
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
        text = "".join(f.text for f in buffered if isinstance(f, LLMTextFrame))
        st = self._injector

        # Gate 1: medical advice
        if contains_medical_advice(text):
            await self._emit(MEDICAL_SAFE_RESPONSE, direction)
            return

        # Gate 2: closed after confirmation
        if st.booking_closed:
            await self._emit(GOODBYE_RESPONSE, direction)
            return

        # Gate 3: caller confirmed → commit once through the port
        if st.caller_confirmed and not self._booking_done:
            self._booking_done = True
            await self._emit(await self._commit_and_confirm(), direction)
            return

        if st.caller_confirmed and self._booking_done:
            await self._emit(BOOKING_NOTED_RESPONSE, direction)
            return

        # Gate 4: force deterministic readback if the model skipped it
        readback = st.booking.format_readback()
        if readback is not None and "correct" not in text.lower():
            await self._emit(readback, direction)
            return

        for f in [*buffered, frame]:
            await self.push_frame(f, direction)

    async def _commit_and_confirm(self) -> str:
        """Commit through the single path and produce receipt-derived speech.

        Never claims success without a receipt: on any failure the caller is
        told to confirm with the clinic, and no success language is spoken.
        """
        from . import clinic_resolver

        bc = self._injector.booking
        if bc.selected_time is None or bc.target_date is None:
            return "Details incomplete. Clinic staff கிட்ட confirm பண்ணுங்க."

        try:
            async with self._resolver.session_factory() as session:
                outcome = await clinic_resolver.book_appointment(
                    command_port=self._resolver.command_port,
                    session=session,
                    business_id=self._resolver.business_id,
                    service_phrase=bc.reason or "",
                    target_date=bc.target_date,
                    target_time=bc.selected_time,
                    idempotency_key=f"voice-{id(self)}-{bc.target_date}-{bc.selected_time}",
                )
        except Exception as exc:
            logger.error("commit_error: %s", type(exc).__name__)
            return "Details note பண்ணிட்டேன். Clinic staff confirm பண்ணுவாங்க."

        if outcome.success:
            logger.info("booking_committed appointment_id=%s", outcome.appointment_id)
            return (
                f"Appointment #{outcome.appointment_id} confirm ஆயிடுச்சு. "
                "வேற ஏதாவது doubt இருக்கா?"
            )
        logger.warning("booking_refused: %s", outcome.error)
        return "இந்த நேரம் book பண்ண முடியல. Clinic-ல call பண்ணி confirm பண்ணுங்க."

    async def _emit(self, text: str, direction: FrameDirection):
        await self.push_frame(LLMFullResponseStartFrame(), direction)
        await self.push_frame(LLMTextFrame(text=text), direction)
        await self.push_frame(LLMFullResponseEndFrame(), direction)
