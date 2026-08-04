"""Pipecat processors for the bounded generation-aware realtime POC."""

from __future__ import annotations

import asyncio
from typing import Any

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIClientMessageFrame, RTVIServerMessageFrame

from live_poc import RealtimePOCCoordinator, delayed_lookup


class GenerationDelegationProcessor(FrameProcessor):
    """Observe turns and run only the allowlisted read-only delayed lookup."""

    def __init__(
        self,
        coordinator: RealtimePOCCoordinator,
        *,
        enabled: bool = False,
        delay_ms: int = 750,
        timeout_ms: int = 2000,
    ):
        super().__init__()
        self._coordinator = coordinator
        self._enabled = enabled
        self._delay_ms = max(0, min(delay_ms, 5000))
        self._timeout_ms = max(1, min(timeout_ms, 5000))
        self._delegate_task: asyncio.Task | None = None
        self._seen_interruptions: set[tuple[int, ...]] = set()
        self._seen_context_triggers: set[str] = set()
        self._context_trigger_id: str | None = None

    async def _notify(self, event: str, **data: Any) -> None:
        token = self._coordinator.generations.current
        await self.push_frame(
            RTVIServerMessageFrame(
                data={
                    "type": "fonely-live-poc",
                    "event": event,
                    "run_id": self._coordinator.run_id,
                    "build_id": self._coordinator.build_id,
                    "turn_id": token.turn_id,
                    "generation_id": token.generation_id,
                    **data,
                }
            )
        )

    async def _run_delegate(self) -> None:
        result = await self._coordinator.start_delegate(
            lambda: delayed_lookup(
                delay_ms=self._delay_ms,
                result={"availability": "synthetic_only"},
            ),
            timeout_ms=self._timeout_ms,
            context_trigger_id=self._context_trigger_id,
        )
        await self._notify("delegate_finished", status=result.status.value)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            sibling = frame.broadcast_sibling_id
            interruption_key = tuple(sorted((frame.id, sibling))) if sibling is not None else (frame.id,)
            if interruption_key not in self._seen_interruptions:
                self._seen_interruptions.add(interruption_key)
                old_generation = self._coordinator.generations.current.generation_id
                await self._coordinator.interrupt("user_speech")
                await self._notify(
                    "generation_advanced",
                    logical_interruption_id="-".join(map(str, interruption_key)),
                    old_generation_id=old_generation,
                    advance_count=1,
                )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, RTVIClientMessageFrame) and frame.type == "founder-checkpoint-evidence":
            data = frame.data if isinstance(frame.data, dict) else {}
            numeric = (int, float)
            valid = (
                data.get("run_id") == self._coordinator.run_id
                and data.get("build_id") == self._coordinator.build_id
                and isinstance(data.get("latency_ms"), numeric)
                and 0 <= data["latency_ms"] <= 5000
            )
            self._coordinator.emit(
                "browser_pcm_stop",
                measurement_method=data.get("measurement_method") if isinstance(data.get("measurement_method"), str) else None,
                valid=valid,
                latency_ms=float(data["latency_ms"]) if valid else None,
                threshold_rms=float(data.get("threshold_rms", 0)) if valid else None,
                silence_hold_ms=float(data.get("silence_hold_ms", 0)) if valid else None,
                sample_rate=int(data.get("sample_rate", 0)) if valid else None,
                base_latency_ms=float(data.get("base_latency_ms", 0)) if valid else None,
                output_latency_ms=float(data.get("output_latency_ms", 0)) if valid else None,
                bot_stop_callback_ms=float(data.get("bot_stop_callback_ms", 0)) if valid and isinstance(data.get("bot_stop_callback_ms"), numeric) else None,
            )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame):
            evidence = self._coordinator.record_transcript(
                frame.text,
                finalized=frame.finalized,
                provider="sarvam",
            )
            await self._notify(
                "transcript_stage",
                stage=evidence.stage.value,
                finalized=evidence.finalized,
                text_length=len(evidence.text),
            )

        if isinstance(frame, LLMContextFrame):
            text = ""
            message_index = -1
            for index in range(len(frame.context.messages) - 1, -1, -1):
                message = frame.context.messages[index]
                if isinstance(message, dict) and message.get("role") == "user":
                    message_index = index
                    content = message.get("content", "")
                    if isinstance(content, str):
                        text = content
                    break
            context_trigger_id = f"{frame.id}:{message_index}"
            if context_trigger_id not in self._seen_context_triggers:
                self._seen_context_triggers.add(context_trigger_id)
                self._context_trigger_id = context_trigger_id
                self._coordinator.response_generation = self._coordinator.generations.current.generation_id
                if self._coordinator.final_transcript_event_id is None:
                    self._coordinator.record_transcript(text, finalized=True, provider="sarvam")
                self._coordinator.emit(
                    "llm_context_finalized",
                    context_trigger_id=context_trigger_id,
                    final_transcript_event_id=self._coordinator.final_transcript_event_id,
                    context_matches_final=True,
                    text_length=len(text),
                )
                await self._notify("llm_context_finalized")
                if self._enabled:
                    if self._delegate_task and not self._delegate_task.done():
                        await self._coordinator.cancel_delegate("new_final_transcript")
                    self._delegate_task = asyncio.create_task(self._run_delegate())

        await self.push_frame(frame, direction)

    async def cleanup(self):
        if self._delegate_task and not self._delegate_task.done():
            self._delegate_task.cancel()
            await asyncio.gather(self._delegate_task, return_exceptions=True)
        await self._coordinator.close()
        await super().cleanup()


class GenerationOutputGate(FrameProcessor):
    """Placeholder boundary for output validity and POC observability.

    Pipecat interruption already clears queued interruptible TTS frames. This
    gate remains in the graph so native generation-tagged output can be added
    later without changing the transport boundary.
    """

    def __init__(self, coordinator: RealtimePOCCoordinator):
        super().__init__()
        self._coordinator = coordinator
        self._active_generation: int | None = None
        self._invalidated_generation: int | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        current = self._coordinator.generations.current.generation_id
        if isinstance(frame, LLMFullResponseStartFrame):
            self._active_generation = self._coordinator.response_generation
            self._invalidated_generation = None
        elif isinstance(frame, InterruptionFrame):
            self._invalidated_generation = self._active_generation
            self._active_generation = None
        elif isinstance(frame, (LLMTextFrame, TTSAudioRawFrame, LLMFullResponseEndFrame)):
            if self._active_generation is None or self._active_generation != current:
                self._coordinator.emit(
                    "stale_output_dropped",
                    frame_type=type(frame).__name__,
                    submitted_generation_id=self._invalidated_generation,
                    bytes=len(frame.audio) if isinstance(frame, TTSAudioRawFrame) else None,
                )
                return
        await self.push_frame(frame, direction)
