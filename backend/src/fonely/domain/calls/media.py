"""Canonical audio seam contract — typed media events.

Shared between provider adapter (Dev1) and voice runtime (Dev4).
No provider-specific types cross this boundary.
Contract version: 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

INBOUND_SAMPLE_RATE = 16_000
INBOUND_FRAME_MS = 20
INBOUND_SAMPLES_PER_FRAME = INBOUND_SAMPLE_RATE * INBOUND_FRAME_MS // 1000
INBOUND_BYTES_PER_FRAME = INBOUND_SAMPLES_PER_FRAME * 2  # s16le = 2 bytes/sample = 640

OUTBOUND_SAMPLE_RATE = 24_000
OUTBOUND_FRAME_MS = 20
OUTBOUND_SAMPLES_PER_FRAME = OUTBOUND_SAMPLE_RATE * OUTBOUND_FRAME_MS // 1000
OUTBOUND_BYTES_PER_FRAME = OUTBOUND_SAMPLES_PER_FRAME * 2  # = 960

INBOUND_QUEUE_MAX_FRAMES = 50  # 1000 ms / 20 ms
OUTBOUND_QUEUE_MAX_FRAMES = 50
BACKPRESSURE_DEADLINE_MS = 250
UTTERANCE_MAX_FRAMES = 750  # 15 seconds

CONTRACT_VERSION = 1


@dataclass(frozen=True, slots=True)
class AudioFormat:
    encoding: Literal["s16le"]
    sample_rate: int
    channels: Literal[1]
    frame_duration_ms: int
    bytes_per_frame: int


CANONICAL_INBOUND = AudioFormat(
    encoding="s16le",
    sample_rate=INBOUND_SAMPLE_RATE,
    channels=1,
    frame_duration_ms=INBOUND_FRAME_MS,
    bytes_per_frame=INBOUND_BYTES_PER_FRAME,
)

CANONICAL_OUTBOUND = AudioFormat(
    encoding="s16le",
    sample_rate=OUTBOUND_SAMPLE_RATE,
    channels=1,
    frame_duration_ms=OUTBOUND_FRAME_MS,
    bytes_per_frame=OUTBOUND_BYTES_PER_FRAME,
)


@dataclass(frozen=True, slots=True)
class MediaSessionIdentity:
    schema_version: Literal[1]
    session_id: UUID
    business_id: int
    provider: Literal["exotel"]
    provider_environment: Literal["sandbox", "production"]
    provider_account_id: str
    provider_call_id: str
    provider_stream_id: str


class MediaEvent:
    """Base for all typed media events crossing the seam."""


@dataclass(frozen=True, slots=True)
class SessionStarted(MediaEvent):
    identity: MediaSessionIdentity
    input_format: AudioFormat
    output_format: AudioFormat
    started_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class InboundAudioFrame(MediaEvent):
    sequence: int
    media_timestamp_ms: int
    received_monotonic_ns: int
    pcm_s16le_16khz_mono: bytes


@dataclass(frozen=True, slots=True)
class InboundDiscontinuity(MediaEvent):
    expected_sequence: int
    received_sequence: int
    reason: Literal["sequence_gap", "provider_reset"]


@dataclass(frozen=True, slots=True)
class ProviderStreamEnded(MediaEvent):
    reason: Literal[
        "normal_disconnect",
        "provider_stop",
        "network_error",
        "protocol_error",
        "capacity_exceeded",
        "shutdown",
    ]
    provider_code: str | None


@dataclass(frozen=True, slots=True)
class OutboundAudioFrame:
    generation_id: int
    sequence: int
    media_timestamp_ms: int
    pcm_s16le_24khz_mono: bytes


@dataclass(frozen=True, slots=True)
class ClearOutboundAudio:
    generation_id: int
    reason: Literal["barge_in", "cancelled", "superseded", "shutdown"]


@dataclass(frozen=True, slots=True)
class OutboundGenerationEnded:
    generation_id: int
    final_sequence: int
