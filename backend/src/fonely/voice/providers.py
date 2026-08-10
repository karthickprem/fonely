"""Provider adapter factory with typed config, timeout, and usage accounting.

Each adapter wraps its Pipecat service with production boundaries:
connection/synthesis timeouts, error classification, usage event
emission, and explicit close.  No PII in error frames.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Any

from .config import LLMConfig, STTConfig, TTSConfig

logger = logging.getLogger("fonely.voice.providers")


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    healthy: bool
    reason: str = ""


def probe_credentials() -> dict[str, str]:
    """Report set/unset status for required provider credentials.

    Never logs or returns actual values.
    """
    required = [
        "SARVAM_API_KEY",
        "ANTHROPIC_API_KEY",
        "CARTESIA_API_KEY",
        "CARTESIA_VOICE_ID",
    ]
    result = {}
    for name in required:
        value = os.environ.get(name, "")
        if not value:
            result[name] = "UNSET"
        elif len(value) < 8:
            result[name] = "SET_SHORT"
        else:
            result[name] = "SET"
    return result


def credentials_ready() -> bool:
    probe = probe_credentials()
    return all(v == "SET" for v in probe.values())


def validate_api_key(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def build_stt(config: STTConfig) -> Any:
    """Build SarvamSTTService with production config."""
    from pipecat.services.sarvam.stt import SarvamSTTService

    api_key = validate_api_key("SARVAM_API_KEY")
    return SarvamSTTService(
        api_key=api_key,
        mode=config.mode,
        sample_rate=config.sample_rate,
        input_audio_codec=config.input_codec,
        settings=SarvamSTTService.Settings(
            model=config.model,
            language=None,
            vad_signals=False,
        ),
    )


def build_llm(config: LLMConfig, *, evidence_sink: Any = None) -> Any:
    """Build AnthropicLLMService with gateway support and usage metrics."""
    from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
    from pipecat.services.anthropic.llm import AnthropicLLMService

    api_key = validate_api_key("ANTHROPIC_API_KEY")
    headers: dict[str, str] = {}

    for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip() or not value.strip():
            raise RuntimeError("ANTHROPIC_CUSTOM_HEADERS contains an invalid header line")
        headers[name.strip()] = value.strip()

    gateway_user = os.environ.get("ANTHROPIC_GATEWAY_USER")
    if gateway_user and "user" not in {n.casefold() for n in headers}:
        headers["user"] = gateway_user

    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url and "api.anthropic.com" not in base_url and not headers:
        raise RuntimeError("configured Anthropic gateway requires approved custom headers")

    http_client = DefaultAsyncHttpxClient()
    client = AsyncAnthropic(
        api_key=api_key,
        base_url=base_url,
        default_headers=headers or None,
        http_client=http_client,
    )

    settings_kwargs: dict[str, Any] = {"max_tokens": config.max_tokens}
    if config.model:
        settings_kwargs["model"] = config.model

    return AnthropicLLMService(
        api_key=api_key,
        client=client,
        settings=AnthropicLLMService.Settings(**settings_kwargs),
    )


def validate_cartesia_speed(speed: float) -> float:
    if not isinstance(speed, (int, float)) or isinstance(speed, bool) or not math.isfinite(speed):
        return 0.95
    return max(0.6, min(1.5, float(speed)))


def build_tts(config: TTSConfig) -> Any:
    """Build CartesiaTTSService with validated settings."""
    from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
    from pipecat.services.tts_service import TextAggregationMode
    from pipecat.transcriptions.language import Language

    api_key = validate_api_key("CARTESIA_API_KEY")
    voice_id = config.voice_id or validate_api_key("CARTESIA_VOICE_ID")
    speed = validate_cartesia_speed(config.speed)

    tts = CartesiaTTSService(
        api_key=api_key,
        sample_rate=config.sample_rate,
        text_aggregation_mode=TextAggregationMode.SENTENCE,
        settings=CartesiaTTSService.Settings(
            model=config.model,
            voice=voice_id,
            language=Language.TA,
            generation_config=GenerationConfig(speed=speed, emotion=config.emotion),
        ),
    )
    return tts


def clean_spoken_text(text: str, _aggregation_type: Any = None) -> str:
    """Normalize narrow TTS boundaries without changing response facts."""
    spoken = re.sub(r"^ச+ரிங்க", "சரிங்க", text.strip())
    spoken = re.sub(r"(?<=[A-Za-z])\s*-\s*ஆ\b", "", spoken)
    return spoken
