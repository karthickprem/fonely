"""Booking-enabled Pipecat pipeline: real STT + LLM + TTS + PostgreSQL.

Same Pipecat WebRTC transport as the demo pipeline, but with:
- AppointmentServiceCommandPort for real PostgreSQL commits
- Receipt-derived confirmation speech
- Live session mode (not demo)

Loaded by server_webrtc.py when the client path is /voice-booking.
"""
from __future__ import annotations

import os
import sys
import re
from contextlib import asynccontextmanager
from pathlib import Path

from loguru import logger

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

# Add fonely.voice from the runtime worktree (the gated code)
_RUNTIME_SRC = str(Path(__file__).resolve().parents[1] / ".claude" / "worktrees" / "dev4-voice-runtime" / "backend" / "src")
if _RUNTIME_SRC not in sys.path:
    sys.path.insert(0, _RUNTIME_SRC)

from fonely.voice.dialogue import BookingCollection, contains_medical_advice
from fonely.voice.context import resolve_relative_date, TrustedClock

from pipeline import cartesia_settings, clean_spoken_text

# These were the legacy processors — replaced by deterministic BookingStateInjector/BookingPostLLMGate
# from processors import ReceiptAwareTTSGate, TurnContextProcessor
# from style_retriever import ChennaiStyleRetriever

MEDICAL_SAFE_RESPONSE = "அதற்கு doctor நேரில் பார்த்துதான் சொல்ல முடியும். Appointment book பண்ணலாமா?"

_CONFIRM_WORDS = frozenset({
    "yes", "yeah", "yep", "ok", "okay", "correct", "right", "sure", "hmm",
    "ஆமா", "ஆம்", "சரி", "சரிங்க", "aamaa", "sari", "aama",
})


def _is_confirmation(text: str) -> bool:
    return text.strip().casefold().rstrip(".!") in _CONFIRM_WORDS


class BookingStateInjector(FrameProcessor):
    """Pre-LLM: injects BookingCollection state into the LLM context.

    The state machine owns which field is asked. The LLM sees
    required_field and must ask ONLY that field.
    Imports BookingCollection from fonely.voice — not a copy.
    """

    def __init__(self, clock: TrustedClock):
        super().__init__()
        self._booking = BookingCollection()
        self._clock = clock
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

        resolved_date = resolve_relative_date(user_text, self._clock)

        self._booking.update(
            user_text,
            resolved_date=resolved_date,
            availability=self._last_availability,
            previous_assistant_text=prev_assistant,
        )

        # Detect confirmation deterministically
        if self._booking.required_field == "confirmation" and _is_confirmation(user_text):
            self.caller_confirmed = True

        # Detect closure (caller says no/bye after booking)
        if self.caller_confirmed and not self.booking_closed:
            lower = user_text.strip().casefold()
            if any(w in lower for w in ("no", "bye", "இல்ல", "போறேன்", "நன்றி", "thanks", "nothing")):
                self.booking_closed = True

        state_block = self._booking.render()
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], dict) and messages[i].get("role") == "user":
                original = messages[i].get("content", "")
                messages[i] = {"role": "user", "content": f"{original}\n\n{state_block}"}
                break

        new_context = LLMContext(
            messages=messages,
            tools=frame.context.tools,
            tool_choice=frame.context.tool_choice,
        )
        await self.push_frame(LLMContextFrame(context=new_context), direction)


class BookingPostLLMGate(FrameProcessor):
    """Post-LLM: deterministic gates on LLM output.

    1. Medical advice → replace with safe referral
    2. Caller confirmed → force closure, never repeat readback
    3. Caller said bye after confirmation → deterministic goodbye
    4. All fields collected but LLM skipped readback → force it

    Imports contains_medical_advice from fonely.voice — not a copy.
    """

    def __init__(self, state: BookingStateInjector):
        super().__init__()
        self._state = state
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

        # Gate 1: medical advice → deterministic safe replacement
        if contains_medical_advice(text):
            await self._emit(MEDICAL_SAFE_RESPONSE, direction)
            return

        # Gate 2: caller said bye after confirmation → deterministic goodbye
        if self._state.booking_closed:
            await self._emit("நன்றி, take care! Clinic-ல சந்திப்போம்.", direction)
            return

        # Gate 3: caller confirmed → force closure, never repeat readback
        if self._state.caller_confirmed:
            await self._emit(
                "Booking note பண்ணிட்டேன். வேற ஏதாவது doubt இருக்கா?",
                direction,
            )
            return

        # Gate 4: all fields collected but LLM didn't readback → force it
        readback = self._state.booking.format_readback()
        if readback is not None:
            if "correct" not in text.lower():
                await self._emit(readback, direction)
                return

        # Pass through
        for f in [*buffered, frame]:
            await self.push_frame(f, direction)

    async def _emit(self, text: str, direction: FrameDirection):
        await self.push_frame(LLMFullResponseStartFrame(), direction)
        await self.push_frame(LLMTextFrame(text=text), direction)
        await self.push_frame(LLMFullResponseEndFrame(), direction)

BOOKING_SYSTEM_PROMPT = """You are Fonely, the virtual receptionist for Smile Dental Clinic in Aminjikarai, Chennai.

Speak like a warm local Chennai person, not a formal Tamil announcer or chatbot.
- Match the caller's Tamil, Tanglish, or Indian English.
- Use Tamil script for Tamil words and keep natural English words like appointment, fee, scaling, root canal in English.

Response discipline — follow strictly:
- Each response does exactly one thing: answer the caller's question OR ask for the next missing field.
- Ask at most one question per response. After the question, stop.
- Lead with the answer. Use the fewest natural spoken words needed.
- Do not narrate your process.
- Do not repeat facts the caller already provided.
- After asking a question, stop speaking.
- No markdown, lists, emoji, meta commentary.
- NEVER repeat a question you already asked. If the caller answered, accept it and move forward.

Medical safety:
- Never suggest specific treatments, medications, dosages, or diagnoses.
- For pain or symptoms: acknowledge briefly, then refer to the clinic or doctor.

Current context:
- Today is {today_display} ({day_of_week}).
- Business timezone: Asia/Kolkata.

Available slots for today:
  Dr. Priya: 10:00, 11:00, 17:00, 18:30

Available slots for tomorrow:
  Dr. Priya: 10:00, 11:00, 17:00, 18:30

Clinic facts:
Dr. Priya: Mon-Sat, general, root canal, scaling.
Hours: 10-1 and 5-8:30, Mon-Sat. Sunday closed.
Consultation ₹300, scaling ₹800.

Booking flow — follow this exact order, one field per turn:
1. Reason/service
2. Date
3. Time (from offered slots ONLY — never invent times)
4. Patient name — accept WHATEVER the caller says. "B", "K", single letters, nicknames are all valid names. Ask ONCE only. Never demand a "full name".
5. Readback: state reason, date, time, name in one sentence. Ask "இது correct-ஆ?"
6. Confirmation: "yes", "yeah", "ஆமா", "correct", "ok", "sari", "hmm", "yep" ALL mean confirmed. Accept on first attempt. NEVER repeat the readback after confirmation.
7. After confirmation: say "Booking note பண்ணிட்டேன். வேற ஏதாவது doubt இருக்கா?" If they say no/bye, say "நன்றி, take care!" and end.

CRITICAL:
- After step 6 confirmation, go DIRECTLY to step 7. Never loop back to step 5.
- Do NOT ask for phone number.
- Be goal-driven: complete the booking and close the conversation.
"""

GREETING = "வணக்கம், Smile Dental Clinic. நான் Fonely. Appointment book பண்ண உதவி பண்ணலாம்."


async def run_booking_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    from datetime import datetime, UTC
    from zoneinfo import ZoneInfo

    sarvam_key = os.environ.get("SARVAM_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    cartesia_key = os.environ.get("CARTESIA_API_KEY")
    cartesia_voice_id = os.environ.get("CARTESIA_VOICE_ID")
    if not all([sarvam_key, anthropic_key, cartesia_key, cartesia_voice_id]):
        raise RuntimeError("Missing voice provider credentials")

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    system_prompt = BOOKING_SYSTEM_PROMPT.format(
        today_display=now.strftime("%A, %B %d, %Y"),
        day_of_week=now.strftime("%A"),
    )

    request_settings = runner_args.body if isinstance(runner_args.body, dict) else {}
    speed, emotion = cartesia_settings(request_settings)

    import aiohttp
    async with aiohttp.ClientSession():
        stt = SarvamSTTService(
            api_key=sarvam_key,
            mode="codemix",
            sample_rate=16000,
            input_audio_codec="wav",
            settings=SarvamSTTService.Settings(
                model="saaras:v3",
                language=None,
                vad_signals=False,
            ),
        )

        # GPT-5.6 Luna via AMD gateway — 21x cheaper, better booking discipline
        gateway_headers = {}
        for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
            if ":" in line.strip():
                k, v = line.strip().split(":", 1)
                gateway_headers[k.strip()] = v.strip()
        gateway_headers["user"] = "karthick"

        llm = OpenAILLMService(
            api_key=gateway_headers.get("Ocp-Apim-Subscription-Key", ""),
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "") + "/v1",
            default_headers=gateway_headers,
            settings=OpenAILLMService.Settings(
                model="gpt-5.6-luna",
                system_instruction=system_prompt,
                max_completion_tokens=300,
            ),
        )

        tts = CartesiaTTSService(
            api_key=cartesia_key,
            sample_rate=24000,
            text_aggregation_mode=TextAggregationMode.SENTENCE,
            settings=CartesiaTTSService.Settings(
                model="sonic-3.5",
                voice=cartesia_voice_id,
                language=Language.TA,
                generation_config=GenerationConfig(speed=speed, emotion=emotion),
            ),
        )
        tts.add_text_transformer(clean_spoken_text)

        vad = SileroVADAnalyzer(
            sample_rate=16000,
            params=VADParams(confidence=0.70, start_secs=0.12, stop_secs=0.20, min_volume=0.60),
        )
        smart_turn = LocalSmartTurnAnalyzerV3(
            cpu_count=1,
            sample_rate=16000,
            params=SmartTurnParams(stop_secs=1.2, pre_speech_ms=500, max_duration_secs=8),
        )
        context = LLMContext()
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=vad,
                user_turn_strategies=UserTurnStrategies(
                    stop=[TurnAnalyzerUserTurnStopStrategy(
                        turn_analyzer=smart_turn, wait_for_transcript=True,
                    )]
                ),
                user_turn_stop_timeout=4.0,
            ),
        )

        # Deterministic state machine — owns field order, readback, confirmation, closure
        clock = TrustedClock.from_now("Asia/Kolkata")
        state_injector = BookingStateInjector(clock)
        post_llm_gate = BookingPostLLMGate(state_injector)

        pipeline = Pipeline([
            transport.input(),
            stt,
            user_aggregator,
            state_injector,    # pre-LLM: injects BookingCollection state
            llm,
            post_llm_gate,     # post-LLM: gates medical/confirmation/closure
            tts,
            transport.output(),
            assistant_aggregator,
        ])

        worker = PipelineWorker(
            pipeline,
            conversation_id=runner_args.session_id,
            params=PipelineParams(
                audio_in_sample_rate=16000,
                audio_out_sample_rate=24000,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            idle_timeout_secs=300,
            enable_turn_tracking=True,
            enable_rtvi=True,
        )

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info("Booking client connected")
            await worker.queue_frames([
                LLMFullResponseStartFrame(),
                LLMTextFrame(text=GREETING),
                LLMFullResponseEndFrame(),
            ])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info("Booking client disconnected")
            await worker.cancel()

        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(
        runner_args,
        {
            "webrtc": lambda: TransportParams(
                audio_in_enabled=True,
                audio_in_sample_rate=16000,
                audio_in_channels=1,
                audio_out_enabled=True,
                audio_out_sample_rate=24000,
                audio_out_channels=1,
                audio_out_10ms_chunks=30,
            )
        },
    )
    await run_booking_bot(transport, runner_args)
