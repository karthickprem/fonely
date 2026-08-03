from __future__ import annotations

import json
import time
from pathlib import Path

from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, ErrorFrame, InterruptionFrame, MetricsFrame, TranscriptionFrame, TTSAudioRawFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
from pipecat.metrics.metrics import TTFAMetricsData, TTFBMetricsData, TurnMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed


class VoiceEvalObserver(BaseObserver):
    """Append small allow-listed evidence records; never audio or transcript text."""

    def __init__(self, *, output_path: Path, session_id: str):
        super().__init__()
        self._output_path = output_path
        self._session_id = session_id
        self._started = time.monotonic_ns()
        self._seen: set[tuple] = set()
        self._dropped = 0
        self._closed = False

    async def on_pipeline_started(self):
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._emit({"event": "session_started"})

    def _emit(self, event: dict):
        event = {"schema_version": 1, "session_id": self._session_id, "offset_ms": (time.monotonic_ns() - self._started) / 1_000_000, **event}
        try:
            with self._output_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
        except Exception:
            self._dropped += 1

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        dedupe_id = (
            ("broadcast", *sorted((frame.id, frame.broadcast_sibling_id)))
            if frame.broadcast_sibling_id is not None
            else ("frame", frame.id)
        )
        if dedupe_id in self._seen:
            return
        self._seen.add(dedupe_id)
        event = None
        if isinstance(frame, VADUserStartedSpeakingFrame): event = {"event": "vad_start", "start_secs": frame.start_secs}
        elif isinstance(frame, VADUserStoppedSpeakingFrame): event = {"event": "vad_stop", "stop_secs": frame.stop_secs}
        elif isinstance(frame, UserStartedSpeakingFrame): event = {"event": "user_turn_start"}
        elif isinstance(frame, UserStoppedSpeakingFrame): event = {"event": "user_turn_stop"}
        elif isinstance(frame, TranscriptionFrame): event = {"event": "stt_transcript", "text_length": len(frame.text), "language": str(frame.language) if frame.language else None, "finalized": frame.finalized}
        elif isinstance(frame, TTSAudioRawFrame): event = {"event": "tts_audio", "bytes": len(frame.audio), "sample_rate": frame.sample_rate}
        elif isinstance(frame, BotStartedSpeakingFrame): event = {"event": "bot_start_proxy"}
        elif isinstance(frame, BotStoppedSpeakingFrame): event = {"event": "bot_stop_proxy"}
        elif isinstance(frame, InterruptionFrame): event = {"event": "interruption"}
        elif isinstance(frame, ErrorFrame): event = {"event": "error", "category": type(frame.exception).__name__ if frame.exception else "provider_error"}
        elif isinstance(frame, MetricsFrame):
            for metric in frame.data:
                if isinstance(metric, TTFBMetricsData) and metric.value > 0:
                    self._emit({"event": "ttfb", "processor": metric.processor, "model": metric.model, "seconds": metric.value})
                elif isinstance(metric, TTFAMetricsData) and metric.ttfa > 0:
                    self._emit({"event": "ttfa", "processor": metric.processor, "model": metric.model, "seconds": metric.ttfa, "leading_silence": metric.leading_silence})
                elif isinstance(metric, TurnMetricsData):
                    self._emit({"event": "smart_turn", "processor": metric.processor, "complete": metric.is_complete, "probability": metric.probability, "processing_ms": metric.e2e_processing_time_ms})
        if event:
            self._emit(event)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._emit({"event": "session_closed", "dropped_events": self._dropped})
