from __future__ import annotations

import time
from dataclasses import dataclass

from pipecat.frames.frames import ErrorFrame, InputAudioRawFrame, TranscriptionFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.tests.utils import SleepFrame, run_test


@dataclass(frozen=True)
class SaarasObservation:
    transcript: str
    language: str | None
    confidence: float | None
    wall_ms: float
    errors: list[str]


async def transcribe_fixture(pcm16: bytes, *, mode: str, api_key: str) -> SaarasObservation:
    if mode not in {"transcribe", "codemix"}:
        raise ValueError(f"Unsupported Saaras mode: {mode}")
    stt = SarvamSTTService(
        api_key=api_key,
        mode=mode,
        sample_rate=16000,
        input_audio_codec="wav",
        settings=SarvamSTTService.Settings(model="saaras:v3", language=None, vad_signals=False),
    )
    frames = [VADUserStartedSpeakingFrame()]
    for offset in range(0, len(pcm16), 640):
        chunk = pcm16[offset:offset + 640]
        if chunk:
            frames.append(InputAudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1))
    frames += [VADUserStoppedSpeakingFrame(), SleepFrame(sleep=2.5)]
    started = time.monotonic()
    down, up = await run_test(stt, frames_to_send=frames)
    wall_ms = (time.monotonic() - started) * 1000
    transcripts = [frame for frame in down if isinstance(frame, TranscriptionFrame)]
    errors = [frame.error for frame in [*down, *up] if isinstance(frame, ErrorFrame)]
    text = " ".join(frame.text for frame in transcripts).strip()
    latest = transcripts[-1] if transcripts else None
    result = latest.result if latest else None
    if isinstance(result, dict):
        data = result.get("data", result)
        confidence = data.get("language_probability") if isinstance(data, dict) else None
        provider_metrics = data.get("metrics", {}) if isinstance(data, dict) else {}
        processing_latency = provider_metrics.get("processing_latency") if isinstance(provider_metrics, dict) else None
        if isinstance(processing_latency, (int, float)):
            wall_ms = processing_latency * 1000
    else:
        confidence = getattr(result, "language_probability", None)
    language = str(latest.language) if latest and latest.language else None
    return SaarasObservation(text, language, confidence, wall_ms, errors)
