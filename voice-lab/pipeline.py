"""Fonely continuous voice pipeline built on Pipecat 1.7."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

import aiohttp
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from loguru import logger

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from delegation import GenerationDelegationProcessor, GenerationOutputGate
from live_poc import RealtimePOCCoordinator
from processors import DentalSafetyProcessor, TurnContextProcessor
from style_retriever import ChennaiStyleRetriever
from voice_eval.observer import VoiceEvalObserver

STYLE_CORPUS = Path(__file__).resolve().parent / "data" / "chennai_dental_style.json"

SYSTEM_PROMPT = """You are Fonely, the virtual receptionist for the synthetic Smile Dental Clinic in Aminjikarai, Chennai.

Speak like a warm local Chennai person, not a formal Tamil announcer or chatbot.
- Match the caller's Tamil, Tanglish, or Indian English. Use Tamil script for Tamil words and keep natural English words like doctor, appointment, fee, front teeth, canine, premolar, molar, scaling, and root canal in English.
- Do not translate familiar English dental words into formal Tamil.

Response discipline — follow strictly:
- Each response does exactly one thing: answer the caller's question OR ask for the next missing field. Not both unless the caller asked a tangent during booking.
- Ask at most one question per response. After the question, stop. Do not add filler, options, or follow-up offers.
- Lead with the answer. Use the fewest natural spoken words needed. One or two short sentences is the target.
- Do not narrate your process ("I'll note that", "Let me check", "Sure, I can help").
- Do not repeat facts the caller already provided.
- Do not offer unsolicited options, alternatives, or "anything else?" unless the caller's request failed.
- After asking a question, stop speaking. Silence is better than filler.
- For tangents during booking: answer in one sentence, then ask one booking-resumption question. Nothing more.
- After demo_complete or abandoned: acknowledge once and stop. No continued prompting.
- No markdown, lists, emoji, meta commentary, Telugu script, or unrelated language.
- This is a demo: never claim a booking was stored, confirmed, a doctor alerted, or staff connected.
- Turn-local <dialogue_state> is trusted routing guidance only. Follow must_not_offer_slot.
- Turn-local <chennai_style_references> guide rhythm only. Never copy facts, names, actions, slots, or promises.

Clinic facts, only when relevant:
Dr. Priya: Mon-Sat, general, root canal, scaling, extraction.
Dr. Arjun: Mon/Wed/Fri, orthodontics, general.
Hours: 10-1 and 5-8:30, Mon-Sat. Sunday closed.
Consultation ₹300; root canal ₹3500-5500; scaling ₹800; extraction ₹500-1500.
Tomorrow: 10, 11, 5, 6:30, 7:30.

Call goal:
- When the caller asks to book, keep one active booking goal until details are read back and explicitly confirmed, the caller abandons it, or staff handoff is required.
- Collect only: reason/service, preferred date, preferred time from offered slots, and patient name.
- Ask exactly one missing field per turn. After asking, stop.
- If the caller asks a side question while booking: answer in one sentence, then ask the one booking-resumption question. Nothing more.
- Never end with "anything else?", "வேற ஏதாவது", thanks, or goodbye while the booking goal is active.
- Once all fields are collected, give one concise readback (reason, date, time, name) and ask "இது correct-ஆ?" — nothing more.
- This lab cannot save appointments. After explicit confirmation, say once that details were collected but not saved, direct to clinic staff, and stop. No continued prompting.
- If the requested slot is unavailable, name the nearest alternatives and ask which one. One sentence.

Dialogue policy:
- General dental education: answer briefly with safe basic information; do not diagnose or recommend treatment.
- Tooth types: front teeth are incisors, pointed teeth are canines, then premolars, and back teeth are molars.
- Booking procedure: explain that the lab can collect details and read them back, but cannot actually store a booking.
- Explicit topic change such as timing வேண்டாம், வேற question, or நான் என்ன கேக்குறேன்: answer the new request first; resume an active booking only through booking_goal.
- Location or fee question outside a booking: answer only what was asked; do not append a slot offer.
- Never assign a slot to a doctor unless explicitly supported by facts.
- Pain: acknowledge discomfort; if a booking goal is active, continue with its required field instead of opening a separate symptom interview.

Good responses — notice one question max, no filler, no narration:
Caller: "Clinic எங்க இருக்கு? Appointment book பண்ணனும்."
Fonely: "Aminjikarai-ல இருக்கு. என்ன reason-க்காக visit பண்ணணும்?"
Caller: "பல்லு வலிக்குது, cleaning பண்ணனும். Insurance claim முடியுமா?"
Fonely: "Insurance claim clinic staff கிட்ட check பண்ணிக்கணும். எந்த date-ல வரணும்?"
Caller: "நாளைக்கு."
Fonely: "நாளைக்கு 10, 11, 5, 6:30, 7:30 available. எந்த time?"
Caller: "6:30."
Fonely: "பேரு சொல்லுங்க?"
Caller: "Karthick."
Fonely: "Cleaning, நாளைக்கு 6:30, Karthick. இது correct-ஆ?"
Caller: "ஆம்."
Fonely: "Details collect பண்ணிட்டேன், ஆனா save ஆகல. Clinic staff கிட்ட confirm பண்ணிக்கோங்க."
"""

GREETING = "வணக்கம், Smile Dental Clinic. நான் Fonely virtual receptionist. எப்படி help பண்ணலாம்?"


async def clean_spoken_text(text: str, _aggregation_type) -> str:
    """Normalize narrow TTS boundaries without changing response facts."""
    spoken = re.sub(r"^ச+ரிங்க", "சரிங்க", text.strip())
    spoken = re.sub(r"(?<=[A-Za-z])\s*-\s*ஆ\b", "", spoken)
    return spoken


def cartesia_settings(request_settings: dict) -> tuple[float, str]:
    """Return bounded, reviewed Cartesia synthesis controls."""
    speed = request_settings.get("speed", 0.95)
    if (
        not isinstance(speed, (int, float))
        or isinstance(speed, bool)
        or not math.isfinite(float(speed))
    ):
        speed = 0.95
    speed = max(0.6, min(1.5, float(speed)))
    emotion = request_settings.get("emotion", "calm")
    if emotion != "calm":
        emotion = "calm"
    return speed, emotion


def build_anthropic_client(
    api_key: str,
    *,
    evidence_sink=None,
) -> AsyncAnthropic:
    """Build the official SDK client and prove headers without recording values."""
    headers = {}
    for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if not separator or not name.strip() or not value.strip():
            raise RuntimeError("ANTHROPIC_CUSTOM_HEADERS contains an invalid header line")
        headers[name.strip()] = value.strip()
    trusted_user = os.environ.get("ANTHROPIC_GATEWAY_USER")
    if trusted_user and "user" not in {name.casefold() for name in headers}:
        headers["user"] = trusted_user
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url and "api.anthropic.com" not in base_url and not headers:
        raise RuntimeError("configured Anthropic gateway requires approved custom headers")
    ordinal = 0

    async def prove_headers(request):
        nonlocal ordinal
        if request.method != "POST" or not request.url.path.endswith("/messages"):
            return
        ordinal += 1
        if ordinal != 1 or evidence_sink is None:
            return
        request_headers = {name.casefold(): value for name, value in request.headers.items()}
        expected = {name.casefold(): value for name, value in headers.items()}
        evidence_sink(
            {
                "event": "claude_request_headers_proved",
                "request_ordinal": ordinal,
                "method": request.method,
                "path": request.url.path,
                "expected_header_names": sorted(expected),
                "required_headers_present": all(name in request_headers for name in expected),
                "configured_values_matched": all(request_headers.get(name) == value for name, value in expected.items()),
                "stream": True,
            }
        )

    http_client = DefaultAsyncHttpxClient(event_hooks={"request": [prove_headers]})
    return AsyncAnthropic(
        api_key=api_key,
        base_url=base_url,
        default_headers=headers or None,
        http_client=http_client,
    )


def build_cartesia_tts(api_key: str, voice_id: str, speed: float, emotion: str):
    return CartesiaTTSService(
        api_key=api_key,
        sample_rate=24000,
        text_aggregation_mode=TextAggregationMode.SENTENCE,
        settings=CartesiaTTSService.Settings(
            model="sonic-3.5",
            voice=voice_id,
            language=Language.TA,
            generation_config=GenerationConfig(speed=speed, emotion=emotion),
        ),
    )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    sarvam_key = os.environ.get("SARVAM_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    cartesia_key = os.environ.get("CARTESIA_API_KEY")
    cartesia_voice_id = os.environ.get("CARTESIA_VOICE_ID")
    if not all([sarvam_key, anthropic_key, cartesia_key, cartesia_voice_id]):
        raise RuntimeError(
            "SARVAM_API_KEY, ANTHROPIC_API_KEY, CARTESIA_API_KEY, and "
            "CARTESIA_VOICE_ID are required"
        )

    request_settings = runner_args.body if isinstance(runner_args.body, dict) else {}
    checkpoint_run_id = request_settings.get("checkpoint_run_id") or os.environ.get("VOICE_CHECKPOINT_RUN_ID")
    checkpoint_build_id = os.environ.get("VOICE_CHECKPOINT_BUILD_ID")
    if checkpoint_run_id and not re.fullmatch(r"[a-f0-9-]{32,36}", checkpoint_run_id):
        raise RuntimeError("checkpoint_run_id must be UUID-shaped")
    speed, emotion = cartesia_settings(request_settings)
    poc_enabled = request_settings.get("live_poc", False) is True
    delegate_delay_ms = request_settings.get("delegate_delay_ms", 750)
    if not isinstance(delegate_delay_ms, int) or isinstance(delegate_delay_ms, bool):
        delegate_delay_ms = 750

    observer = None
    data_root = os.environ.get("VOICE_EVAL_DATA_ROOT")
    if data_root:
        root = Path(data_root).resolve()
        worktree = Path(__file__).resolve().parents[1]
        if root == worktree or root.is_relative_to(worktree):
            raise RuntimeError("VOICE_EVAL_DATA_ROOT must be outside the Git worktree")
        observer = VoiceEvalObserver(
            output_path=root / "telemetry" / f"{runner_args.session_id}.jsonl",
            session_id=runner_args.session_id,
        )
    coordinator = RealtimePOCCoordinator(
        runner_args.session_id,
        run_id=checkpoint_run_id,
        build_id=checkpoint_build_id,
        event_sink=observer.emit_poc if observer else None,
    )
    coordinator.emit("startup_milestone", milestone="bot_factory_entered", warm_state="cold")

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
        def claude_evidence(event):
            payload = dict(event)
            name = payload.pop("event")
            coordinator.emit(name, **payload)

        llm = AnthropicLLMService(
            api_key=anthropic_key,
            client=build_anthropic_client(
                anthropic_key,
                evidence_sink=claude_evidence,
            ),
            settings=AnthropicLLMService.Settings(
                model="claude-opus-4-6",
                system_instruction=SYSTEM_PROMPT,
                max_tokens=1024,
                temperature=0.2,
            ),
        )
        tts = build_cartesia_tts(
            api_key=cartesia_key,
            voice_id=cartesia_voice_id,
            speed=speed,
            emotion=emotion,
        )
        tts.add_text_transformer(clean_spoken_text)

        vad = SileroVADAnalyzer(
            sample_rate=16000,
            params=VADParams(
                confidence=0.70,
                start_secs=0.12,
                stop_secs=0.20,
                min_volume=0.60,
            ),
        )
        smart_turn = LocalSmartTurnAnalyzerV3(
            cpu_count=1,
            sample_rate=16000,
            params=SmartTurnParams(
                stop_secs=1.2,
                pre_speech_ms=500,
                max_duration_secs=8,
            ),
        )
        context = LLMContext()
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=vad,
                user_turn_strategies=UserTurnStrategies(
                    stop=[
                        TurnAnalyzerUserTurnStopStrategy(
                            turn_analyzer=smart_turn,
                            wait_for_transcript=True,
                        )
                    ]
                ),
                user_turn_stop_timeout=4.0,
            ),
        )

        delegation = GenerationDelegationProcessor(
            coordinator,
            enabled=poc_enabled,
            delay_ms=delegate_delay_ms,
        )
        turn_context = TurnContextProcessor(ChennaiStyleRetriever(STYLE_CORPUS))
        safety = DentalSafetyProcessor(booking_tracker=turn_context._booking_goal)
        output_gate = GenerationOutputGate(coordinator)
        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                user_aggregator,
                delegation,
                safety,
                turn_context,
                llm,
                tts,
                output_gate,
                transport.output(),
                assistant_aggregator,
            ]
        )
        coordinator.emit("startup_milestone", milestone="pipeline_assembled", warm_state="cold")
        worker = PipelineWorker(
            pipeline,
            conversation_id=runner_args.session_id,
            observers=[observer] if observer else None,
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

        @worker.event_handler("on_pipeline_finished")
        async def on_pipeline_finished(worker, frame):
            if observer:
                await observer.close()

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info("Voice-lab client connected")
            coordinator.emit("startup_milestone", milestone="webrtc_connected", warm_state="cold")
            coordinator.emit("startup_milestone", milestone="greeting_queued", warm_state="cold")
            await worker.queue_frames(
                [
                    LLMFullResponseStartFrame(),
                    LLMTextFrame(text=GREETING),
                    LLMFullResponseEndFrame(),
                ]
            )

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info("Voice-lab client disconnected")
            await worker.cancel()
            if observer:
                await observer.close()

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
    await run_bot(transport, runner_args)
