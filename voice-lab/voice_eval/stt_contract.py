"""Provider-neutral evidence events for STT experiments."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class STTEventType(StrEnum):
    SPEECH_STARTED = "speech_started"
    SPEECH_ENDED = "speech_ended"
    TRANSCRIPT_PARTIAL = "transcript_partial"
    TRANSCRIPT_FINAL = "transcript_final"
    TRANSCRIPT_ALTERNATIVE = "transcript_alternative"
    LANGUAGE_DETECTED = "language_detected"
    CRITICAL_ENTITY_CANDIDATE = "critical_entity_candidate"
    PROVIDER_ERROR = "provider_error"
    STREAM_CLOSED = "stream_closed"
    USAGE_RECORD = "usage_record"


@dataclass(frozen=True)
class STTEvent:
    schema_version: int
    event_type: STTEventType
    session_id: str
    turn_id: str
    generation_id: str
    provider: str
    model: str
    provider_request_id: str | None = None
    raw_text: str | None = None
    normalized_candidate: str | None = None
    detected_language: str | None = None
    language_confidence: float | None = None
    is_final: bool | None = None
    audio_start_ms: float | None = None
    audio_end_ms: float | None = None
    transcript_start_ms: float | None = None
    transcript_end_ms: float | None = None
    word_timings: tuple[dict[str, Any], ...] = ()
    raw_confidence: float | None = None
    alternatives: tuple[dict[str, Any], ...] = ()
    first_partial_latency_ms: float | None = None
    first_final_latency_ms: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    estimated_cost: dict[str, Any] = field(default_factory=dict)
    provider_error: dict[str, Any] = field(default_factory=dict)
    reconnect: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["event_type"] = self.event_type.value
        return record
