"""Production voice pipeline builder.

Assembles the Pipecat processor chain with typed ports:
TrustedClock, AvailabilityPort, ValidatorPort, and
production prompt architecture.  No hardcoded slots or
static facts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import SpeechClass, VoiceSessionConfig
from .context import AvailabilityPort, DayAvailability, StubAvailabilityPort, TrustedClock
from .generation import GenerationClock
from .prompts import build_greeting, build_system_prompt
from .telemetry import VoiceTelemetryExporter
from .validator_port import FailClosedValidatorStub, ValidationDecision, ValidatorPort

logger = logging.getLogger("fonely.voice.pipeline")


@dataclass(frozen=True)
class PipelineContext:
    """Immutable context for one pipeline invocation."""
    config: VoiceSessionConfig
    clock: TrustedClock
    availability: DayAvailability | None
    system_prompt: str
    greeting: str
    session_mode: str


def build_pipeline_context(
    config: VoiceSessionConfig,
    *,
    clock: TrustedClock | None = None,
    availability_port: AvailabilityPort | None = None,
    clinic_name: str = "Smile Dental Clinic",
    clinic_context: str = "",
    session_mode: str = "demo",
) -> PipelineContext:
    """Build immutable pipeline context from typed ports."""
    if clock is None:
        clock = TrustedClock.from_now("Asia/Kolkata")

    availability: DayAvailability | None = None
    if availability_port is not None:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            pass
        else:
            availability = asyncio.run(
                availability_port.query_day_availability(
                    config.business_id,
                    clock.business_date,
                )
            )

    system_prompt = build_system_prompt(
        clock=clock,
        clinic_name=clinic_name,
        clinic_context=clinic_context,
        availability=availability,
        session_mode=session_mode,
    )

    greeting = build_greeting(clinic_name)

    return PipelineContext(
        config=config,
        clock=clock,
        availability=availability,
        system_prompt=system_prompt,
        greeting=greeting,
        session_mode=session_mode,
    )


class PreTTSValidatorGate:
    """Gate before TTS: blocks consequential speech via ValidatorPort.

    Non-consequential speech passes through.  Consequential speech
    (committed confirmation, notification, handoff, medical) is
    blocked until an independently accepted validator is injected.
    """

    def __init__(
        self,
        validator: ValidatorPort,
        telemetry: VoiceTelemetryExporter,
        clock: GenerationClock,
    ) -> None:
        self._validator = validator
        self._telemetry = telemetry
        self._clock = clock

    def check(self, text: str, speech_class: SpeechClass) -> bool:
        """Returns True if text may proceed to TTS."""
        token = self._clock.current()
        result = self._validator.validate_speech(
            text,
            speech_class,
            session_id=token.session_id,
            turn_id=str(token.turn_id),
            generation_id=token.generation_id,
        )
        self._telemetry.emit(
            "pre_tts_gate",
            decision=result.decision,
            speech_class=speech_class,
            source=result.source,
        )
        if result.decision == ValidationDecision.BLOCK:
            self._telemetry.emit(
                "pre_tts_blocked",
                reason=result.reason,
                speech_class=speech_class,
            )
            return False
        return True


class PostTTSGenerationGate:
    """Gate after TTS: drops stale audio when generation has advanced."""

    def __init__(
        self,
        clock: GenerationClock,
        telemetry: VoiceTelemetryExporter,
    ) -> None:
        self._clock = clock
        self._telemetry = telemetry
        self._dropped_count = 0

    def should_emit(self, generation_id: int) -> bool:
        current = self._clock.current()
        if generation_id != current.generation_id:
            self._dropped_count += 1
            self._telemetry.emit(
                "post_tts_dropped",
                stale_generation=generation_id,
                current_generation=current.generation_id,
            )
            return False
        return True

    @property
    def dropped_count(self) -> int:
        return self._dropped_count
