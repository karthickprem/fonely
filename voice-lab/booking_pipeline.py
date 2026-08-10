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

# Add fonely backend to path for imports
backend_src = str(Path(__file__).resolve().parents[1] / "backend" / "src")
if backend_src not in sys.path:
    sys.path.insert(0, backend_src)

from pipeline import build_anthropic_client, cartesia_settings, clean_spoken_text
from processors import ReceiptAwareTTSGate, TurnContextProcessor
from style_retriever import ChennaiStyleRetriever


STYLE_CORPUS = Path(__file__).resolve().parent / "data" / "chennai_dental_style.json"

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

Current context:
- Today is {today_display} ({day_of_week}).
- Business timezone: Asia/Kolkata.
- Collect booking details and read them back, but do not claim success yourself. Only application commit evidence can authorize booking-success speech.

Available slots for tomorrow:
  Dr. Priya: 10:00-10:30 (scaling)
  Dr. Priya: 18:30-19:00 (scaling)

Clinic facts:
Dr. Priya: Mon-Sat, general, root canal, scaling.
Hours: 10-1 and 5-8:30, Mon-Sat. Sunday closed.
Consultation ₹300, scaling ₹800.

Booking flow:
- When the caller asks to book, collect: reason/service, preferred date, preferred time, and patient name.
- Ask exactly one missing field per turn.
- Once all fields are collected, give one concise readback and ask "இது correct-ஆ?"
- After explicit confirmation ("ஆமா", "yes", "correct"), do not invent success. The application adapter must confirm and provide the committed receipt; without it, say the booking is not confirmed.
- Use ONLY the available slots shown above. Never invent or guess times.

Good responses:
Caller: "Appointment book pannanum"
Fonely: "என்ன reason-க்காக visit?"
Caller: "Scaling"
Fonely: "எந்த date-ல வரணும்?"
Caller: "நாளைக்கு"
Fonely: "நாளைக்கு Dr. Priya கிட்ட 10:00, 18:30 available. எந்த time?"
Caller: "6:30"
Fonely: "பேரு சொல்லுங்க?"
Caller: "Karthick"
Fonely: "Scaling, நாளைக்கு 6:30, Karthick. இது correct-ஆ?"
Caller: "ஆமா"
Fonely: "Booking இன்னும் confirm ஆகல. Clinic staff கிட்ட verify பண்ணிக்கோங்க."
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

        llm = AnthropicLLMService(
            api_key=anthropic_key,
            client=build_anthropic_client(anthropic_key),
            settings=AnthropicLLMService.Settings(
                model="claude-opus-4-6",
                system_instruction=system_prompt,
                max_tokens=300,
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

        turn_context = TurnContextProcessor(ChennaiStyleRetriever(STYLE_CORPUS))
        receipt_gate = ReceiptAwareTTSGate(None, business_id=1)
        pipeline = Pipeline([
            transport.input(),
            stt,
            user_aggregator,
            turn_context,
            llm,
            receipt_gate,
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
