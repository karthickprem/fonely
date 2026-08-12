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
_RUNTIME_SRC = "/scratch/karthick/fonely/.claude/worktrees/dev4-voice-runtime/backend/src"
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
        self._trusted_clock = clock
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

        resolved_date = resolve_relative_date(user_text, self._trusted_clock)

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

        # Inject LIVE clinic context from PostgreSQL
        try:
            from db_backend import get_clinic_context as _get_ctx
            ctx_text = await _get_ctx()
            live_context = f"\n<live_clinic_context>\n{ctx_text}\n</live_clinic_context>\n"

            # If no availability confirmed, ask the doctor
            if "No confirmed availability" in ctx_text and "availability" in user_text.lower() or "slot" in user_text.lower() or "available" in user_text.lower() or "time" in user_text.lower() or "அவைலபிள" in user_text.lower() or "நேரம்" in user_text.lower():
                from doctor_bridge import BRIDGE
                await BRIDGE.ask_doctor(
                    "Patient is asking about today's availability. What are your available slots today?",
                    patient_context=user_text[:100],
                )
        except Exception as _e:
            logger.warning(f"Failed to get DB context: {_e}")
            live_context = ""

        state_block = self._booking.render()
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], dict) and messages[i].get("role") == "user":
                original = messages[i].get("content", "")
                messages[i] = {"role": "user", "content": f"{original}\n\n{live_context}\n{state_block}"}
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

    def __init__(self, state: BookingStateInjector, *, book_fn=None):
        super().__init__()
        self._state = state
        self._book_fn = book_fn
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

        # Gate 1: medical advice → deterministic safe replacement
        if contains_medical_advice(text):
            await self._emit(MEDICAL_SAFE_RESPONSE, direction)
            return

        # Gate 2: caller said bye after confirmation → deterministic goodbye
        if self._state.booking_closed:
            await self._emit("நன்றி, take care! Clinic-ல சந்திப்போம்.", direction)
            return

        # Gate 3: caller confirmed → book in DB, then closure
        if self._state.caller_confirmed and not self._booking_done:
            self._booking_done = True
            confirm_text = "Booking note பண்ணிட்டேன். வேற ஏதாவது doubt இருக்கா?"
            if self._book_fn:
                try:
                    bc = self._state.booking
                    result = await self._book_fn(
                        service_name=bc.reason or "scaling",
                        target_date=bc.target_date,
                        target_time=bc.selected_time,
                        patient_name=bc.patient_name or "Unknown",
                    )
                    if result.get("success"):
                        appt_id = result.get("appointment_id", "?")
                        logger.info(f"Booking committed: appointment #{appt_id}")
                        confirm_text = f"Appointment #{appt_id} confirm ஆயிடுச்சு. வேற ஏதாவது doubt இருக்கா?"
                    else:
                        err = result.get("error", "unknown")
                        logger.warning(f"Booking failed: {err}")
                        confirm_text = f"Booking try பண்ணேன், ஆனா {err}. Clinic-ல call பண்ணி confirm பண்ணுங்க."
                except Exception as e:
                    logger.error(f"Booking error: {e}")
                    confirm_text = "Details note பண்ணிட்டேன். Clinic staff confirm பண்ணுவாங்க."
            await self._emit(confirm_text, direction)
            return

        if self._state.caller_confirmed and self._booking_done:
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

BOOKING_SYSTEM_PROMPT = """You are Fonely, the receptionist at Smile Care Dental Clinic, Adyar, Chennai. You talk like a real Chennai receptionist on the phone — casual, warm, spoken Tamil mixed with English. NOT textbook Tamil, NOT a news reader.

LANGUAGE — this is the most important rule:
- Speak COLLOQUIAL spoken Chennai Tamil (பேச்சு தமிழ்), never formal/literary/written Tamil.
- Keep everyday English words in English: appointment, doctor, book, time, slot, morning, evening, fee, cancel, confirm, scaling, cleaning.
- Match the caller: if they speak English, reply in English. If Tamil/Tanglish, reply in Tanglish (Tamil script + English words), the way people actually text and talk in Chennai.

SAY IT LIKE THIS (colloquial), NOT like that (formal):
- Say "நாளைக்கு" — NOT "நாளை புதன்கிழமை ஆகஸ்ட் 12-ம் தேதி"
- Say "காலைல 10 மணி, 11 மணி இருக்கு. எந்த நேரம் வேணும்?" — NOT "காலை 9:30 முதல் 12:45 வரை நேரம் உள்ளது"
- Say "சரி, book பண்ணிடலாம்" — NOT "சரி, பதிவு செய்யப்படும்"
- Say "உங்க பேரு சொல்லுங்க" — NOT "தங்களது பெயரைக் கூறவும்"
- Say "doctor இருப்பாங்க" — NOT "மருத்துவர் இருப்பார்"
- Use particles: "-ங்க", "-ல", "பண்ணுங்க", "வேணும்", "இருக்கு" — the way Chennai people actually speak.

Keep it short. One or two spoken sentences. Sound like a person, not a form.

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
- Clinic details, services, prices, and available slots are in the <live_clinic_context> block below.
- ALWAYS use the LATEST clinic context for availability and pricing. The owner may update slots or prices mid-conversation.
- If a slot was removed or the clinic is closed, inform the caller and offer alternatives.

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

# Single-sentence greeting → one TTS call, not three. (SENTENCE aggregation
# split the old three-sentence greeting into three synthesis calls.)
GREETING = "வணக்கம், Smile Care Dental Clinic-ல இருந்து Fonely பேசுறேன், appointment book பண்ண உதவி பண்ணலாம்"

# The LLM actually driving this pipeline: OpenAI-protocol model served over the
# AMD Azure-APIM gateway (llm-api.amd.com), NOT an Anthropic/Claude model. Named
# once here and referenced at the LLM construction site and by /api/pipeline-info
# so the reported model can never drift from the served model.
BOOKING_LLM_MODEL = "gpt-5.6-luna"
BOOKING_LLM_GATEWAY = "amd-azure-apim"


async def run_booking_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    from datetime import datetime, UTC
    from zoneinfo import ZoneInfo

    sarvam_key = os.environ.get("SARVAM_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    cartesia_key = os.environ.get("CARTESIA_API_KEY")
    cartesia_voice_id = os.environ.get("CARTESIA_VOICE_ID")
    if not all([sarvam_key, anthropic_key, cartesia_key, cartesia_voice_id]):
        raise RuntimeError("Missing voice provider credentials")

    # Import the shared clinic context from the server
    try:
        from server_webrtc import CLINIC
    except ImportError:
        from clinic_context import ClinicContext
        CLINIC = ClinicContext()

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
                model=BOOKING_LLM_MODEL,
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

        # PRODUCTION processors from backend/src/fonely/voice — the demo runs
        # exactly the code that ships. Injector + gate + resolver + single
        # commit path + language mirroring all come from the production package.
        from production_wiring import build_processors
        state_injector, post_llm_gate = build_processors(runner_args.session_id)

        # DPDP capture gate (production package). NoticeInputLatch starts CLOSED
        # and drops caller audio until the open order opens it — capture cannot
        # begin before the notice completes and its evidence persists. Without
        # this the pipeline fed transport.input() straight to STT, so a failed
        # (or skipped) notice still captured speech. NoticePlaybackSignal watches
        # the output for the real BotStoppedSpeakingFrame so the open order waits
        # for actual playback, not a guessed sleep.
        from fonely.voice.input_latch import NoticeInputLatch
        from fonely.voice.playback_signal import NoticePlaybackSignal
        input_latch = NoticeInputLatch()
        playback_signal = NoticePlaybackSignal()

        pipeline = Pipeline([
            transport.input(),
            input_latch,       # capture gate: drops caller audio until open()
            stt,
            user_aggregator,
            state_injector,    # pre-LLM: injects BookingCollection state
            llm,
            post_llm_gate,     # post-LLM: gates medical/confirmation/closure
            tts,
            transport.output(),
            playback_signal,   # observes BotStoppedSpeakingFrame (notice done)
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
            # DPDP enforced open order (production package): notice → real
            # playback complete → evidence persisted → greeting → capture opens.
            # The greeting is spoken ONLY on the success path, and the input
            # latch opens ONLY after evidence persists — a failed persist keeps
            # capture CLOSED and the caller hears a short failure line instead of
            # being silently recorded without provable consent.
            from datetime import datetime
            from zoneinfo import ZoneInfo

            from fonely.voice.notice_playback import build_notice_open_sequence
            from fonely.voice.open_order import OpenOutcome
            from fonely.voice.session_open import open_session

            LOCALE = "ta-IN"
            opening = open_session(
                clinic_name="Smile Care Dental Clinic",
                greeting_text=GREETING,
                locale=LOCALE,
            )
            # Create the call row FIRST (real telephony: admission does this),
            # so the SQL evidence writer has a row to UPDATE the dpdp_notice_*
            # columns on. Thread its id into the open sequence.
            call_id = await production_wiring.create_call_row(
                conversation_id=runner_args.session_id,
            )
            evidence_writer = production_wiring.build_notice_evidence_writer()

            def _make_speech_frames(text: str):
                return [
                    LLMFullResponseStartFrame(),
                    LLMTextFrame(text=text),
                    LLMFullResponseEndFrame(),
                ]

            async def _await_playback_complete() -> bool:
                # Wait for the notice to actually finish playing (real
                # BotStoppedSpeakingFrame), not a guessed duration.
                return await playback_signal.await_complete(timeout=30.0)

            open_sequence = build_notice_open_sequence(
                call_id=call_id,  # real calls row; SQL writer UPDATEs its columns
                opening=opening,
                locale=LOCALE,
                queue_frames=worker.queue_frames,
                make_speech_frames=_make_speech_frames,
                await_playback_complete=_await_playback_complete,
                evidence_writer=evidence_writer,
                latch=input_latch,
                now=lambda: datetime.now(ZoneInfo("Asia/Kolkata")),
                failure_line=(
                    "மன்னிக்கவும், தொழில்நுட்ப சிக்கல். "
                    "தயவுசெய்து clinic-ஐ நேரடியாக அழைக்கவும்."
                ),
            )

            result = await open_sequence()
            if result.outcome is not OpenOutcome.OPENED:
                logger.warning(
                    "DPDP open failed (%s) — capture stays CLOSED, tearing down",
                    result.outcome.value,
                )
                await worker.cancel()

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
