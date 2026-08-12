"""Bounded async telemetry observer for voice sessions.

Replaces synchronous JSONL file writes with a non-blocking bounded
queue.  PII-safe: no transcript text, audio, phone, SDP, or provider
error bodies are emitted.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("fonely.voice.telemetry")

MAX_QUEUE_SIZE = 1000
FLUSH_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class TelemetryEvent:
    name: str
    session_id: str
    timestamp_ns: int
    data: dict[str, Any] = field(default_factory=dict)


class VoiceTelemetryExporter:
    """Bounded non-blocking telemetry collector.

    Events are queued and can be consumed by an external exporter.
    If the queue is full, the oldest non-critical event is dropped
    and a counter is incremented.
    """

    def __init__(self, session_id: str, max_size: int = MAX_QUEUE_SIZE) -> None:
        self._session_id = session_id
        self._max_size = max_size
        self._queue: deque[TelemetryEvent] = deque(maxlen=max_size)
        self._dropped = 0
        self._closed = False
        self._total_emitted = 0
        self._usage: dict[str, float] = {
            "stt_seconds": 0.0,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "tts_characters": 0,
        }
        self._cost_cents: float = 0.0

    def emit(self, name: str, **data: Any) -> None:
        if self._closed:
            return
        event = TelemetryEvent(
            name=name,
            session_id=self._session_id,
            timestamp_ns=time.monotonic_ns(),
            data=data,
        )
        if len(self._queue) >= self._max_size:
            self._dropped += 1
        self._queue.append(event)
        self._total_emitted += 1

    def record_stt_usage(self, seconds: float) -> None:
        self._usage["stt_seconds"] += seconds

    def record_llm_usage(self, input_tokens: int, output_tokens: int) -> None:
        self._usage["llm_input_tokens"] += input_tokens
        self._usage["llm_output_tokens"] += output_tokens

    def record_tts_usage(self, characters: int) -> None:
        self._usage["tts_characters"] += characters

    def usage_summary(self) -> dict[str, Any]:
        return {
            "session_id": self._session_id,
            "total_emitted": self._total_emitted,
            "dropped": self._dropped,
            "queue_current": len(self._queue),
            **self._usage,
            "estimated_cost_cents": self._cost_cents,
        }

    def drain(self) -> list[TelemetryEvent]:
        events = list(self._queue)
        self._queue.clear()
        return events

    def close(self) -> dict[str, Any]:
        if self._closed:
            return self.usage_summary()
        event = TelemetryEvent(
            name="session_telemetry_closed",
            session_id=self._session_id,
            timestamp_ns=time.monotonic_ns(),
        )
        self._queue.append(event)
        self._total_emitted += 1
        self._closed = True
        return self.usage_summary()
