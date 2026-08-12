"""Typed immutable voice session and provider configuration."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal

# Sarvam STT recognition modes, matching the provider SDK's accepted values.
# Typing the config field with this Literal makes an invalid mode a type error
# at the source rather than an arg-type error at the provider call site.
SttMode = Literal["transcribe", "translate", "verbatim", "translit", "codemix"]


class SpeechClass(enum.StrEnum):
    NON_CONSEQUENTIAL = "non_consequential"
    COMMITTED_CREATE = "committed_create"
    COMMITTED_CANCEL = "committed_cancel"
    COMMITTED_RESCHEDULE = "committed_reschedule"
    NOTIFICATION_SENT = "notification_sent"
    NOTIFICATION_DELIVERED = "notification_delivered"
    HANDOFF_INITIATED = "handoff_initiated"
    HANDOFF_CONNECTED = "handoff_connected"
    REVIEWED_SAFETY = "reviewed_safety"
    REVIEWED_MEDICAL = "reviewed_medical"


CONSEQUENTIAL_CLASSES = frozenset(SpeechClass) - {SpeechClass.NON_CONSEQUENTIAL}


class SessionState(enum.StrEnum):
    CREATED = "created"
    SIGNALING = "signaling"
    CONNECTING = "connecting"
    ACTIVE = "active"
    RECONNECTING = "reconnecting"
    DRAINING = "draining"
    CLOSED = "closed"
    FAILED = "failed"


_VALID_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.CREATED: frozenset({SessionState.SIGNALING, SessionState.FAILED}),
    SessionState.SIGNALING: frozenset({SessionState.CONNECTING, SessionState.FAILED}),
    SessionState.CONNECTING: frozenset({SessionState.ACTIVE, SessionState.FAILED}),
    SessionState.ACTIVE: frozenset(
        {SessionState.RECONNECTING, SessionState.DRAINING, SessionState.FAILED}
    ),
    SessionState.RECONNECTING: frozenset({SessionState.ACTIVE, SessionState.FAILED}),
    SessionState.DRAINING: frozenset({SessionState.CLOSED, SessionState.FAILED}),
    SessionState.CLOSED: frozenset(),
    SessionState.FAILED: frozenset(),
}


@dataclass(frozen=True)
class STTConfig:
    provider: str = "sarvam"
    model: str = "saaras:v3"
    mode: SttMode = "codemix"
    sample_rate: int = 16000
    input_codec: str = "wav"
    connection_timeout_seconds: float = 10.0
    stream_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "anthropic"
    model: str = ""  # Use project default; no hardcoded model version
    max_tokens: int = 1024
    per_turn_timeout_seconds: float = 15.0
    session_token_budget: int = 50_000


@dataclass(frozen=True)
class TTSConfig:
    provider: str = "cartesia"
    model: str = "sonic-3.5"
    voice_id: str = ""
    language: str = "ta"
    sample_rate: int = 24000
    speed: float = 0.95
    emotion: str = "calm"
    synthesis_timeout_seconds: float = 10.0
    max_response_bytes: int = 5_000_000


@dataclass(frozen=True)
class AudioConfig:
    input_sample_rate: int = 16000
    input_channels: int = 1
    output_sample_rate: int = 24000
    output_channels: int = 1
    output_buffer_10ms_chunks: int = 30
    input_queue_max_ms: int = 5000
    output_queue_max_ms: int = 3000


@dataclass(frozen=True)
class VADConfig:
    confidence: float = 0.70
    start_seconds: float = 0.12
    stop_seconds: float = 0.20
    min_volume: float = 0.60


@dataclass(frozen=True)
class TurnConfig:
    smart_turn_stop_seconds: float = 1.2
    pre_speech_ms: int = 500
    max_duration_seconds: float = 8.0
    turn_stop_timeout_seconds: float = 4.0
    cpu_count: int = 1


@dataclass(frozen=True)
class SessionLimits:
    max_duration_seconds: int = 600
    max_turns: int = 50
    reconnect_grace_seconds: float = 15.0
    idle_timeout_seconds: int = 300
    max_concurrent_per_tenant: int = 10


@dataclass(frozen=True)
class VoiceSessionConfig:
    session_id: str
    business_id: int
    tenant_id: str = ""
    stt: STTConfig = field(default_factory=STTConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VADConfig = field(default_factory=VADConfig)
    turn: TurnConfig = field(default_factory=TurnConfig)
    limits: SessionLimits = field(default_factory=SessionLimits)
