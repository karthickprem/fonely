"""Production voice pipeline builder.

Assembles the Pipecat processor chain with typed ports:
TrustedClock, AvailabilityPort, ValidatorPort, and
production prompt architecture.  No hardcoded slots or
static facts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import SpeechClass, VoiceSessionConfig
from .context import (
    AvailabilityPort,
    AvailabilityQuery,
    DayAvailability,
    TrustedClock,
)
from .generation import GenerationClock
from .prompts import build_greeting, build_system_prompt
from .telemetry import VoiceTelemetryExporter
from .validator_port import ValidationDecision, ValidatorPort

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


async def build_pipeline_context(
    config: VoiceSessionConfig,
    *,
    clock: TrustedClock,
    business_name: str,
    business_context: str = "",
    availability_port: AvailabilityPort | None = None,
    session_mode: str = "demo",
    service_id: int | None = None,
    resource_id: int | None = None,
) -> PipelineContext:
    """Build immutable pipeline context from typed ports.

    Timezone is required through clock — no hardcoded fallback.
    Availability is always awaited when a port is provided.
    """
    availability: DayAvailability | None = None
    if availability_port is not None:
        query = AvailabilityQuery(
            business_id=config.business_id,
            target_date=clock.business_date,
            business_timezone=clock.business_timezone,
            service_id=service_id,
            resource_id=resource_id,
        )
        availability = await availability_port.query_day_availability(query)

    system_prompt = build_system_prompt(
        clock=clock,
        clinic_name=business_name,
        clinic_context=business_context,
        availability=availability,
        session_mode=session_mode,
    )

    greeting = build_greeting(business_name)

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
