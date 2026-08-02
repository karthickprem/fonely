"""Fonely continuous voice pipeline built on Pipecat 1.7."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

import aiohttp
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
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from processors import ChennaiStyleProcessor, DentalSafetyProcessor
from services import SarvamStreamingHttpTTSService
from style_retriever import ChennaiStyleRetriever

STYLE_CORPUS = Path(__file__).resolve().parent / "data" / "chennai_dental_style.json"

SYSTEM_PROMPT = """You are Fonely, the virtual receptionist for the synthetic Smile Dental Clinic in Aminjikarai, Chennai.

Speak like a warm local Chennai receptionist, not a chatbot.
- Match the caller: Tamil, Tanglish, or Indian English.
- Write Tamil words in Tamil script; keep genuine English words in English.
- Maximum 15 spoken words in exactly one sentence.
- Ask exactly one short question per turn.
- Never dump schedules, doctors, or services. Offer at most two slots in one turn.
- Use natural Chennai phrases selectively: சரிங்க, அப்படியா, அட பாவம், okay.
- No markdown, lists, or meta commentary.
- This is a demo: never claim a booking was stored, a doctor was alerted, or staff was connected.
- Turn-local <chennai_style_references> guide rhythm and warmth only. Never copy their facts, names, actions, slots, or promises.
- Reference examples may use Roman Tamil; your spoken output must use Tamil script for Tamil words.

Clinic facts, only when relevant:
Dr. Priya: Mon-Sat, general, root canal, scaling, extraction.
Dr. Arjun: Mon/Wed/Fri, orthodontics, general.
Hours: 10-1 and 5-8:30, Mon-Sat. Sunday closed.
Consultation ₹300; root canal ₹3500-5500; scaling ₹800; extraction ₹500-1500.
Tomorrow: 10, 11, 5, 6:30, 7:30.

Conversation policy:
- Appointment request with no reason: ask only why they need to visit.
- Doctor availability with no named doctor: ask only which doctor they prefer.
- Location question: answer only the location; do not ask about booking.
- Fee question: answer the known fee only; at most ask whether to check a consultation slot.
- Never assign a slot to a specific doctor unless that doctor-slot pairing is explicitly in the facts. It is not currently available.
- Never say a booking is confirmed; this lab has no booking tool.

Good responses:
"சரிங்க, எதுக்கு வரணும் சொல்லுங்க?"
"எந்த doctor வேணும்னு சொல்லுங்க?"
"நம்ம clinic Aminjikarai-ல இருக்கு."
"Scaling ₹800. Consultation slot பாக்கவா?"
"உங்க பேரு சொல்லுங்க."
"Details note பண்ணிட்டேன்; இது demo மட்டும்."
"""

GREETING = "வணக்கம், Smile Dental Clinic. நான் Fonely virtual receptionist. எப்படி help பண்ணலாம்?"


async def clean_spoken_text(text: str, _aggregation_type) -> str:
    """Fix narrow repeated-syllable glitches without rewriting content."""
    return re.sub(r"^ச+ரிங்க", "சரிங்க", text.strip())


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    sarvam_key = os.environ.get("SARVAM_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not sarvam_key or not anthropic_key:
        raise RuntimeError("SARVAM_API_KEY and ANTHROPIC_API_KEY are required")

    request_settings = runner_args.body if isinstance(runner_args.body, dict) else {}
    reviewed_voices = {"priya", "shreya", "ritu", "neha", "roopa"}
    voice = request_settings.get("voice", "priya")
    if not isinstance(voice, str) or voice not in reviewed_voices:
        voice = "priya"

    def bounded_number(name: str, default: float, low: float, high: float) -> float:
        value = request_settings.get(name, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return default
        value = float(value)
        if not math.isfinite(value):
            return default
        return max(low, min(high, value))

    pace = bounded_number("pace", 0.95, 0.5, 2.0)
    temperature = bounded_number("temperature", 0.55, 0.01, 1.0)

    async with aiohttp.ClientSession() as session:
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
            settings=AnthropicLLMService.Settings(
                model="claude-haiku-4-5",
                system_instruction=SYSTEM_PROMPT,
                max_tokens=80,
                temperature=0.2,
            ),
        )
        tts = SarvamStreamingHttpTTSService(
            api_key=sarvam_key,
            aiohttp_session=session,
            voice=voice,
            language=Language.TA_IN,
            pace=pace,
            temperature=temperature,
            sample_rate=24000,
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

        safety = DentalSafetyProcessor()
        style = ChennaiStyleProcessor(ChennaiStyleRetriever(STYLE_CORPUS))
        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                user_aggregator,
                safety,
                style,
                llm,
                tts,
                transport.output(),
                assistant_aggregator,
            ]
        )
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
            logger.info("Voice-lab client connected")
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
            )
        },
    )
    await run_bot(transport, runner_args)
