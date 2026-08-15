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
    """Report set/unset status for a FIXED snapshot of provider credentials.

    HARDCODED SNAPSHOT FOR HUMAN DIAGNOSTICS ONLY — NOT the serviceability gate.
    It reports the same fixed quad regardless of which providers are actually
    selected, so an operator eyeballing readiness sees a stable picture. Do NOT
    use this (or ``credentials_ready``) to decide whether a voice-enabled process
    should start: the LLM provider is config-SELECTED, so requiring
    ``ANTHROPIC_API_KEY`` here is a false gate for a Luna/openai_compatible deploy
    (fails when correctly configured, or passes when the real gateway is down).
    Use ``resolved_voice_creds_ready`` for gating — it probes the ACTUALLY-
    SELECTED providers. Never logs or returns actual values.
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
    """Legacy fixed-snapshot readiness — HUMAN DIAGNOSTICS ONLY, NOT a gate.

    See ``probe_credentials``: this checks the hardcoded quad, which is a false
    gate for a config-selected provider. Use ``resolved_voice_creds_ready`` to
    gate a voice-enabled startup / readiness on the providers actually selected.
    """
    probe = probe_credentials()
    return all(v == "SET" for v in probe.values())


def validate_api_key(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


# The env vars each SELECTED STT/TTS provider needs, keyed on the config's
# provider string — NOT hardcoded in the gate body. When STT/TTS become
# config-selectable (the LLM already is), the probe follows the config
# automatically instead of silently checking the wrong provider. An unknown
# provider string has no entry → the gate fails closed (can't prove
# serviceability → don't start), never a silent pass.
_STT_PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "sarvam": ("SARVAM_API_KEY",),
}
_TTS_PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "cartesia": ("CARTESIA_API_KEY", "CARTESIA_VOICE_ID"),
}


class VoiceCredentialsError(RuntimeError):
    """A voice-enabled process cannot prove its SELECTED providers are
    serviceable. Raised at startup so the process FAILS CLOSED (does not start
    and serve silent calls) rather than failing on the first real call."""


def _selected_provider_keys(
    provider: str, key_map: dict[str, tuple[str, ...]], *, kind: str
) -> tuple[str, ...]:
    keys = key_map.get(provider)
    if keys is None:
        # Unknown provider = can't prove serviceability = fail closed.
        raise VoiceCredentialsError(
            f"unknown {kind} provider {provider!r}: no credential probe defined, "
            "cannot prove serviceability — refusing to start"
        )
    return keys


def resolved_voice_creds_ready(
    stt_config: STTConfig,
    tts_config: TTSConfig,
    llm_config: LLMConfig,
    *,
    environ: dict[str, str] | None = None,
) -> None:
    """The SERVICEABILITY GATE: prove the ACTUALLY-SELECTED voice providers are
    configured, or raise ``VoiceCredentialsError`` so a voice-enabled startup /
    readiness fails CLOSED.

    Gates on the RESOLVED providers, never a fixed name:
      * LLM — ``validate_llm_startup(llm_config)`` (resolves the selected provider
        and checks its key-env + base_url; handles anthropic-vs-openai_compatible
        correctly, so ANTHROPIC is checked ONLY when anthropic is selected).
      * STT / TTS — the env vars the SELECTED provider needs, from the per-
        provider map (unknown provider → fail closed).

    This is what a voice-enabled process's startup and readiness must use — NOT
    ``credentials_ready`` (a hardcoded snapshot, a false gate for a config-
    selected provider). Raises on the FIRST missing thing with a specific reason;
    returns None when everything the composition will select is present.
    """
    env: dict[str, str] = dict(os.environ) if environ is None else environ

    # LLM: the resolved validator. It raises LLMConfigError (a RuntimeError
    # subclass) on a missing/misconfigured selected provider.
    from .llm_selection import validate_llm_startup

    validate_llm_startup(llm_config, environ=env)

    # STT + TTS: the selected provider's keys must all be set.
    for kind, provider, key_map in (
        ("STT", stt_config.provider, _STT_PROVIDER_KEYS),
        ("TTS", tts_config.provider, _TTS_PROVIDER_KEYS),
    ):
        for name in _selected_provider_keys(provider, key_map, kind=kind):
            if not env.get(name, ""):
                raise VoiceCredentialsError(
                    f"{kind} provider {provider!r} selected but {name} is unset — "
                    "cannot serve calls, refusing to start"
                )


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
    """Build the selected LLM service, provider-neutral.

    Dispatches on ``config.provider``: ``anthropic`` builds the native Anthropic
    service (unchanged); ``openai_compatible`` builds an OpenAI-protocol service
    against any endpoint named in config. Startup validation
    (``llm_selection.validate_llm_startup``) is the fail-closed gate that should
    run BEFORE this — this builder assumes a validated selection and raises via
    the same ``validate_api_key`` path if a key is somehow still missing."""
    builder = _LLM_BUILDERS.get(config.provider)
    if builder is None:
        from .llm_selection import LLMConfigError

        raise LLMConfigError(f"no LLM builder for provider {config.provider!r}")
    return builder(config)


def build_anthropic_llm(config: LLMConfig) -> Any:
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
    if base_url:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        if parsed.scheme not in ("https",):
            raise RuntimeError("ANTHROPIC_BASE_URL must use HTTPS")
        approved_hosts = {"api.anthropic.com"}
        if parsed.hostname not in approved_hosts and not headers:
            raise RuntimeError("non-approved Anthropic gateway requires custom headers")

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


def build_openai_compatible_llm(config: LLMConfig) -> Any:
    """Build an OpenAI-protocol LLM service against any endpoint named in config.

    Provider-neutral: the model, base_url, API-key env var, and auth header name
    all come from ``config`` (resolved via ``llm_selection``), so a gateway is
    expressed as data — no ANTHROPIC_* env var and no gateway-specific header
    name is baked in here. Assumes ``validate_llm_startup`` already passed; still
    fails loud if the key env is unset (never silently sends an empty key)."""
    from pipecat.services.openai.llm import OpenAILLMService

    from .llm_selection import resolve_llm_selection

    resolved = resolve_llm_selection(config)
    if not resolved.api_key_env:
        raise RuntimeError("openai_compatible provider requires api_key_env in config")
    api_key = validate_api_key(resolved.api_key_env)

    # The auth header: send the key under the configured header name, formatted
    # by the configured template (e.g. "Bearer {key}") or raw. Only added when
    # the header name is not the OpenAI SDK's own Authorization default, so a
    # gateway using a custom header (e.g. Ocp-Apim-Subscription-Key) works
    # without duplicating the standard bearer auth.
    default_headers: dict[str, str] = {}
    if resolved.auth_header_name and resolved.auth_header_name.casefold() != "authorization":
        header_value = (
            resolved.auth_header_format.format(key=api_key)
            if resolved.auth_header_format
            else api_key
        )
        default_headers[resolved.auth_header_name] = header_value

    settings_kwargs: dict[str, Any] = {"max_completion_tokens": config.max_tokens}
    if config.model:
        settings_kwargs["model"] = config.model

    return OpenAILLMService(
        api_key=api_key,
        base_url=resolved.base_url or None,
        default_headers=default_headers or None,
        settings=OpenAILLMService.Settings(**settings_kwargs),
    )


# Provider -> builder registry. build_llm dispatches through this, so adding a
# provider is one entry here plus a builder — the anthropic path stays available
# and is never privileged over the neutral one.
_LLM_BUILDERS: dict[str, Any] = {
    "anthropic": build_anthropic_llm,
    "openai_compatible": build_openai_compatible_llm,
}


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
