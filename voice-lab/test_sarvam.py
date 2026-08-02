"""Real Sarvam provider gates through Pipecat 1.7 services.

These checks use Pipecat frames and service lifecycle. They do not recreate
Sarvam's WebSocket protocol. Audio stays in memory and no customer data is used.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import dotenv
import numpy as np
import soxr

LAB_DIR = Path(__file__).resolve().parent
dotenv.load_dotenv(LAB_DIR.parent / ".env")

from pipecat.frames.frames import (  # noqa: E402
    ErrorFrame,
    InputAudioRawFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.services.anthropic.llm import AnthropicLLMService  # noqa: E402
from pipecat.services.sarvam.llm import SarvamLLMService  # noqa: E402
from pipecat.services.sarvam.stt import SarvamSTTService  # noqa: E402
from pipecat.services.sarvam.tts import SarvamTTSService  # noqa: E402
from pipecat.services.tts_service import TextAggregationMode  # noqa: E402
from pipecat.tests.utils import SleepFrame, run_test  # noqa: E402
from pipecat.transcriptions.language import Language  # noqa: E402

sys.path.insert(0, str(LAB_DIR))
from services import SarvamStreamingHttpTTSService  # noqa: E402

API_KEY = os.environ.get("SARVAM_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


@dataclass
class TTSResult:
    audio: bytes
    sample_rate: int
    frame_count: int
    total_ms: float


def require_key() -> None:
    if not API_KEY:
        raise RuntimeError("SARVAM_API_KEY is not configured")
    print(f"API key: configured ({len(API_KEY)} characters)")


def frame_errors(frames) -> list[str]:
    return [frame.error for frame in frames if isinstance(frame, ErrorFrame)]


async def smoke_llm() -> bool:
    print("\n=== PIPECAT SARVAM LLM STREAM ===")
    llm = SarvamLLMService(
        api_key=API_KEY,
        settings=SarvamLLMService.Settings(
            model="sarvam-105b",
            temperature=0.6,
            max_tokens=300,
            reasoning_effort=None,
        ),
    )
    context = LLMContext(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a Chennai dental receptionist. Reply in two short "
                    "spoken Tanglish sentences with no markdown."
                ),
            },
            {
                "role": "user",
                "content": "நாளைக்கு evening appointment available-ஆ?",
            },
        ]
    )

    started = time.monotonic()
    down, up = await run_test(
        llm,
        frames_to_send=[LLMContextFrame(context=context)],
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    chunks = [frame.text for frame in down if isinstance(frame, LLMTextFrame)]
    errors = frame_errors(down) + frame_errors(up)
    text = "".join(chunks).strip()

    print(f"Downstream frames: {[type(frame).__name__ for frame in down]}")
    print(f"Upstream frames: {[type(frame).__name__ for frame in up]}")
    print(f"Text chunks: {len(chunks)}")
    print(f"Completion: {elapsed_ms:.0f} ms")
    print(f"Response: {text!r}")
    if errors:
        print(f"Errors: {errors}")
    passed = len(chunks) >= 2 and bool(text) and not errors
    print(f"PIPECAT LLM: {'WORKING' if passed else 'FAILED'}")
    return passed


async def smoke_anthropic_llm() -> bool:
    print("\n=== PIPECAT CLAUDE HAIKU STREAM ===")
    if not ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is missing")
        print("PIPECAT CLAUDE LLM: FAILED")
        return False

    llm = AnthropicLLMService(
        api_key=ANTHROPIC_API_KEY,
        settings=AnthropicLLMService.Settings(
            model="claude-haiku-4-5",
            system_instruction=(
                "You are a warm Chennai dental receptionist. Reply in one short "
                "spoken Tanglish sentence, maximum 15 words, no markdown."
            ),
            max_tokens=80,
            temperature=0.4,
        ),
    )
    context = LLMContext(
        messages=[
            {
                "role": "user",
                "content": "நாளைக்கு evening appointment available-ஆ?",
            }
        ]
    )
    started = time.monotonic()
    down, up = await run_test(llm, frames_to_send=[LLMContextFrame(context=context)])
    elapsed_ms = (time.monotonic() - started) * 1000
    chunks = [frame.text for frame in down if isinstance(frame, LLMTextFrame)]
    errors = frame_errors(down) + frame_errors(up)
    text = "".join(chunks).strip()
    print(f"Text chunks: {len(chunks)}")
    print(f"Completion: {elapsed_ms:.0f} ms")
    print(f"Response: {text!r}")
    if errors:
        print(f"Errors: {errors}")
    passed = len(chunks) >= 1 and bool(text) and not errors
    print(f"PIPECAT CLAUDE LLM: {'WORKING' if passed else 'FAILED'}")
    return passed


async def synthesize_streaming(text: str, voice: str = "priya") -> TTSResult:
    async with aiohttp.ClientSession() as session:
        tts = SarvamStreamingHttpTTSService(
            api_key=API_KEY,
            aiohttp_session=session,
            voice=voice,
        )

        started = time.monotonic()
        down, up = await run_test(
            tts,
            frames_to_send=[
                LLMFullResponseStartFrame(),
                LLMTextFrame(text=text),
                LLMFullResponseEndFrame(),
                SleepFrame(sleep=0.5),
            ],
        )
    total_ms = (time.monotonic() - started) * 1000
    audio_frames = [frame for frame in down if isinstance(frame, TTSAudioRawFrame)]
    stopped = any(isinstance(frame, TTSStoppedFrame) for frame in down)
    errors = frame_errors(down) + frame_errors(up)
    if errors:
        raise RuntimeError("; ".join(errors))
    if not audio_frames:
        raise RuntimeError("Sarvam TTS emitted no audio frames")
    if not stopped:
        raise RuntimeError("Sarvam TTS emitted no completion frame")

    return TTSResult(
        audio=b"".join(frame.audio for frame in audio_frames),
        sample_rate=audio_frames[0].sample_rate,
        frame_count=len(audio_frames),
        total_ms=total_ms,
    )


async def smoke_tts() -> tuple[bool, TTSResult | None]:
    print("\n=== PIPECAT SARVAM TTS STREAM ===")
    text = "நாளைக்கு evening ஆறு முப்பதுக்கு appointment இருக்கு. வரீங்களா?"
    try:
        result = await synthesize_streaming(text)
    except Exception as exc:
        print(f"Error: {exc}")
        print("PIPECAT TTS: FAILED")
        return False, None

    seconds = len(result.audio) / (result.sample_rate * 2)
    print(f"Audio frames: {result.frame_count}")
    print(f"Audio bytes: {len(result.audio)}")
    print(f"Sample rate: {result.sample_rate} Hz mono PCM16")
    print(f"Audio duration: {seconds:.2f} s")
    print(f"Completion: {result.total_ms:.0f} ms")
    passed = result.frame_count > 1 and result.sample_rate == 24000 and len(result.audio) > 0
    print(f"PIPECAT TTS: {'WORKING' if passed else 'FAILED'}")
    return passed, result


def resample_pcm16(audio: bytes, source_rate: int, target_rate: int = 16000) -> bytes:
    samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768.0
    converted = soxr.resample(samples, source_rate, target_rate, quality="HQ")
    converted = np.clip(converted, -1.0, 1.0)
    return (converted * 32767.0).astype("<i2").tobytes()


async def transcribe_streaming(audio_16k: bytes, mode: str) -> tuple[str, list[str], float]:
    stt = SarvamSTTService(
        api_key=API_KEY,
        mode=mode,
        sample_rate=16000,
        input_audio_codec="wav",
        settings=SarvamSTTService.Settings(
            model="saaras:v3",
            language=None,
            vad_signals=False,
        ),
    )

    chunk_bytes = 640  # 20 ms: 16k samples/s * 2 bytes * 0.02
    frames = [VADUserStartedSpeakingFrame()]
    for offset in range(0, len(audio_16k), chunk_bytes):
        chunk = audio_16k[offset : offset + chunk_bytes]
        if chunk:
            frames.append(
                InputAudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1)
            )
    frames.extend([VADUserStoppedSpeakingFrame(), SleepFrame(sleep=1.5)])

    started = time.monotonic()
    down, up = await run_test(stt, frames_to_send=frames)
    elapsed_ms = (time.monotonic() - started) * 1000
    transcripts = [frame.text for frame in down if isinstance(frame, TranscriptionFrame)]
    errors = frame_errors(down) + frame_errors(up)
    return " ".join(transcripts).strip(), errors, elapsed_ms


async def synthesize_rest_fixture(text: str) -> TTSResult:
    """Create a known-good synthetic fixture; STT itself remains Pipecat streaming."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.sarvam.ai/text-to-speech",
            json={
                "text": text,
                "target_language_code": "ta-IN",
                "model": "bulbul:v3",
                "speaker": "priya",
                "speech_sample_rate": 24000,
                "output_audio_codec": "linear16",
                "pace": 0.95,
                "temperature": 0.55,
            },
            headers={
                "Content-Type": "application/json",
                "api-subscription-key": API_KEY,
            },
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"REST fixture TTS HTTP {response.status}: {await response.text()}")
            data = await response.json()
            audio = base64.b64decode(data["audios"][0])
            return TTSResult(audio, 24000, 1, 0)


async def smoke_stt(tts_result: TTSResult | None = None) -> bool:
    print("\n=== PIPECAT SARVAM STT STREAM ===")
    if tts_result is None:
        try:
            tts_result = await synthesize_rest_fixture(
                "Dr. Priya நாளைக்கு evening available-ஆ?"
            )
        except Exception as exc:
            print(f"Unable to create synthetic speech fixture: {exc}")
            print("PIPECAT STT: FAILED")
            return False

    audio_16k = resample_pcm16(tts_result.audio, tts_result.sample_rate)
    passed_modes = 0
    for mode in ("transcribe", "codemix"):
        text, errors, elapsed_ms = await transcribe_streaming(audio_16k, mode)
        print(f"Mode {mode}: {text!r} ({elapsed_ms:.0f} ms)")
        if errors:
            print(f"Mode {mode} errors: {errors}")
        if text and not errors:
            passed_modes += 1

    passed = passed_modes >= 1
    print(f"PIPECAT STT: {'WORKING' if passed else 'FAILED'}")
    return passed


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="Smoke Sarvam-105b")
    parser.add_argument("--claude-llm", action="store_true", help="Smoke Claude Haiku")
    parser.add_argument("--tts", action="store_true")
    parser.add_argument("--stt", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    run_all = args.all or not (args.llm or args.claude_llm or args.tts or args.stt)
    if run_all or args.llm or args.tts or args.stt:
        require_key()
    results: dict[str, bool] = {}
    tts_result: TTSResult | None = None

    if args.llm:
        results["sarvam_llm"] = await smoke_llm()
    if run_all or args.claude_llm:
        results["claude_llm"] = await smoke_anthropic_llm()
    if run_all or args.tts:
        tts_ok, tts_result = await smoke_tts()
        results["tts"] = tts_ok
    if run_all or args.stt:
        # If streaming TTS did not pass, use a known-good REST-generated fixture
        # only to isolate and verify the Pipecat streaming STT service.
        results["stt"] = await smoke_stt(tts_result)

    print("\n=== STREAMING PROVIDER GATE ===")
    for name, passed in results.items():
        print(f"{name.upper()}: {'PASS' if passed else 'FAIL'}")
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
