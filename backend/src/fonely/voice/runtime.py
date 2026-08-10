"""Integrated voice pipeline runtime.

Owns STT/LLM/TTS clients, lifecycle, dialogue state, async
context/availability queries per turn, validator classification,
generation gating, command port, terminal stop, telemetry, and
close.  This is the actual production orchestrator, not scaffolding.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from .config import CONSEQUENTIAL_CLASSES, SessionState, SpeechClass, VoiceSessionConfig
from .context import (
    AvailabilityPort,
    AvailabilityQuery,
    DayAvailability,
    StubAvailabilityPort,
    TrustedClock,
    resolve_relative_date,
)
from .dialogue import DialogueState, count_questions, detect_filler, get_terminal_response
from .generation import GenerationClock
from .lifecycle import VoiceSessionSupervisor
from .prompts import build_system_prompt, format_availability
from .telemetry import VoiceTelemetryExporter
from .validator_port import FailClosedValidatorStub, SpeechValidationResult, ValidationDecision, ValidatorPort

logger = logging.getLogger("fonely.voice.runtime")


class STTPort(Protocol):
    async def transcribe(self, audio: bytes) -> str: ...
    async def close(self) -> None: ...


class LLMPort(Protocol):
    async def generate(self, system: str, messages: list[dict[str, str]]) -> str: ...
    async def close(self) -> None: ...


class TTSPort(Protocol):
    async def synthesize(self, text: str) -> bytes: ...
    async def close(self) -> None: ...


class CommandPort(Protocol):
    """Typed command port for authoritative business mutations.

    Live booking/order requires both an accepted validator AND
    this port.  Without both, mode must be demo with upfront refusal.
    """
    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class TurnResult:
    turn_number: int
    caller_text: str
    response_text: str
    speech_class: SpeechClass
    allowed: bool
    blocked_reason: str = ""
    availability_queried: bool = False
    relative_date_resolved: date | None = None
    filler_detected: bool = False
    question_count: int = 0
    terminal: bool = False
    terminal_reason: str = ""
    elapsed_ms: float = 0.0


class PipelineRuntime:
    """Production voice pipeline runtime owning all provider clients."""

    def __init__(
        self,
        config: VoiceSessionConfig,
        *,
        clock: TrustedClock,
        business_name: str,
        business_context: str = "",
        business_timezone: str,
        stt: STTPort,
        llm: LLMPort,
        tts: TTSPort,
        validator: ValidatorPort | None = None,
        availability_port: AvailabilityPort | None = None,
        command_port: CommandPort | None = None,
        session_mode: str = "demo",
    ) -> None:
        self._config = config
        self._clock = clock
        self._business_name = business_name
        self._business_context = business_context
        self._business_timezone = business_timezone
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._validator = validator or FailClosedValidatorStub()
        self._availability = availability_port or StubAvailabilityPort()
        self._command_port = command_port
        self._session_mode = session_mode

        self._supervisor = VoiceSessionSupervisor(config, validator=self._validator)
        self._gen_clock = GenerationClock(config.session_id)
        self._telemetry = VoiceTelemetryExporter(config.session_id)
        self._dialogue = DialogueState(max_turns=config.limits.max_turns)
        self._messages: list[dict[str, str]] = []
        self._system_prompt = ""
        self._closed = False
        self._turn_results: list[TurnResult] = []
        self._total_tts_bytes = 0
        self._total_stt_calls = 0
        self._total_llm_calls = 0

        if session_mode == "live" and command_port is None:
            self._session_mode = "demo"
            logger.warning("live_mode_downgraded_no_command_port",
                           extra={"session": config.session_id})

    @property
    def supervisor(self) -> VoiceSessionSupervisor:
        return self._supervisor

    @property
    def dialogue(self) -> DialogueState:
        return self._dialogue

    @property
    def turn_results(self) -> list[TurnResult]:
        return list(self._turn_results)

    @property
    def telemetry(self) -> VoiceTelemetryExporter:
        return self._telemetry

    @property
    def generation_clock(self) -> GenerationClock:
        return self._gen_clock

    async def initialize(self) -> None:
        """Build system prompt with current availability."""
        availability = await self._query_availability(self._clock.business_date)

        self._system_prompt = build_system_prompt(
            clock=self._clock,
            clinic_name=self._business_name,
            clinic_context=self._business_context,
            availability=availability,
            session_mode=self._session_mode,
        )

        self._supervisor.transition(SessionState.SIGNALING)
        self._supervisor.transition(SessionState.CONNECTING)
        self._supervisor.transition(SessionState.ACTIVE)

        self._telemetry.emit("runtime_initialized",
                             business_name=self._business_name,
                             session_mode=self._session_mode)

    async def process_turn(self, caller_audio: bytes) -> TurnResult:
        """Process one caller turn through the full pipeline.

        STT → date resolve → availability query → LLM → classify →
        validate → TTS (if allowed) → dialogue state → telemetry.
        """
        if self._closed or self._dialogue.terminal:
            reason = self._dialogue.terminal_reason or "session_closed"
            terminal_text = get_terminal_response(reason)
            return TurnResult(
                turn_number=self._gen_clock.turn_count,
                caller_text="",
                response_text=terminal_text,
                speech_class=SpeechClass.NON_CONSEQUENTIAL,
                allowed=False,
                terminal=True,
                terminal_reason=reason,
            )

        t0 = time.monotonic()
        token = self._gen_clock.next_turn()

        # 1. STT
        caller_text = await self._stt.transcribe(caller_audio)
        self._total_stt_calls += 1
        self._telemetry.record_stt_usage(len(caller_text.split()) * 0.4)

        # 2. Resolve relative dates and query availability per turn
        availability_queried = False
        resolved_date = None
        resolved = resolve_relative_date(caller_text, self._clock)
        if resolved is not None:
            resolved_date = resolved
            availability = await self._query_availability(resolved)
            availability_queried = True
            availability_text = format_availability(availability)
            self._telemetry.emit("availability_queried",
                                 date=str(resolved),
                                 turn=token.turn_id)

        # 3. LLM
        self._messages.append({"role": "user", "content": caller_text})
        response = await self._llm.generate(self._system_prompt, self._messages)
        self._total_llm_calls += 1
        self._telemetry.record_llm_usage(
            len(caller_text.split()),
            len(response.split()),
        )

        # 4. Classify speech and validate
        speech_class = self._classify_speech(response)
        validation = self._validator.validate_speech(
            response,
            speech_class,
            session_id=self._config.session_id,
            turn_id=str(token.turn_id),
            generation_id=token.generation_id,
        )
        allowed = validation.decision == ValidationDecision.ALLOW

        self._telemetry.emit("speech_validated",
                             speech_class=speech_class,
                             decision=validation.decision,
                             source=validation.source)

        # 5. TTS (only if allowed and generation still current)
        if allowed and self._gen_clock.is_current(token):
            tts_audio = await self._tts.synthesize(response)
            self._total_tts_bytes += len(tts_audio)
            self._telemetry.record_tts_usage(len(response))
            self._messages.append({"role": "assistant", "content": response})
        elif not allowed:
            self._telemetry.emit("speech_blocked",
                                 speech_class=speech_class,
                                 reason=validation.reason)

        # 6. Dialogue state
        has_filler = detect_filler(response)
        q_count = count_questions(response)
        can_continue = self._dialogue.record_turn(
            response,
            asked_field=self._infer_asked_field(response),
        )

        terminal = False
        terminal_reason = ""
        if not can_continue or self._dialogue.is_over_budget():
            terminal = True
            terminal_reason = "max_turns" if self._dialogue.is_over_budget() else self._dialogue.terminal_reason
            self._dialogue.set_terminal(terminal_reason)

        elapsed = (time.monotonic() - t0) * 1000

        result = TurnResult(
            turn_number=token.turn_id,
            caller_text=caller_text,
            response_text=response,
            speech_class=speech_class,
            allowed=allowed,
            blocked_reason=validation.reason if not allowed else "",
            availability_queried=availability_queried,
            relative_date_resolved=resolved_date,
            filler_detected=has_filler,
            question_count=q_count,
            terminal=terminal,
            terminal_reason=terminal_reason,
            elapsed_ms=elapsed,
        )
        self._turn_results.append(result)
        return result

    async def close(self, reason: str = "normal") -> dict[str, Any]:
        """Close all owned resources."""
        if self._closed:
            return self._telemetry.usage_summary()
        self._closed = True

        errors: list[str] = []
        for name, client in [("stt", self._stt), ("llm", self._llm), ("tts", self._tts)]:
            try:
                await client.close()
            except Exception as e:
                errors.append(f"{name}:{type(e).__name__}")

        summary = await self._supervisor.close(reason)
        tel_summary = self._telemetry.close()

        return {
            **summary,
            **tel_summary,
            "total_turns": len(self._turn_results),
            "total_stt_calls": self._total_stt_calls,
            "total_llm_calls": self._total_llm_calls,
            "total_tts_bytes": self._total_tts_bytes,
            "close_errors": errors,
        }

    async def _query_availability(self, target_date: date) -> DayAvailability:
        query = AvailabilityQuery(
            business_id=self._config.business_id,
            target_date=target_date,
            business_timezone=self._business_timezone,
        )
        return await self._availability.query_day_availability(query)

    def _classify_speech(self, text: str) -> SpeechClass:
        """Deterministic speech classification from response text.

        Default: NON_CONSEQUENTIAL for safe speech.
        Any consequential claim detected → appropriate class.
        Unclassified suspicious text → NON_CONSEQUENTIAL (validator
        stub will still block if truly consequential).
        """
        import re
        lower = text.lower()

        commit_patterns = [
            r"\b(confirmed|booked|reserved|saved|fixed|scheduled)\b",
            r"(book aayiduchu|fix aayiduchu|confirm aayiduchu|உறுதியாகிவிட்டது|பதிவு செய்யப்பட்டது)",
        ]
        for p in commit_patterns:
            if re.search(p, lower) or re.search(p, text):
                return SpeechClass.COMMITTED_CREATE

        notify_patterns = [
            r"\b(notified|informed|alerted|alert sent|message sent)\b",
            r"(தகவல் அனுப்பப்பட்டது)",
        ]
        for p in notify_patterns:
            if re.search(p, lower) or re.search(p, text):
                return SpeechClass.NOTIFICATION_SENT

        handoff_patterns = [
            r"\b(transferred|connected|call transferred)\b",
            r"(இணைத்துவிட்டேன்)",
        ]
        for p in handoff_patterns:
            if re.search(p, lower) or re.search(p, text):
                return SpeechClass.HANDOFF_CONNECTED

        return SpeechClass.NON_CONSEQUENTIAL

    def _infer_asked_field(self, response: str) -> str | None:
        """Infer which field was asked from response text."""
        import re
        lower = response.lower()
        if re.search(r"(reason|service|என்ன|treatment)", lower):
            if "?" in response or "சொல்லுங்க" in response:
                return "reason"
        if re.search(r"(date|நாள்|தேதி|எப்ப|day)", lower):
            if "?" in response or "வரணும்" in response:
                return "date"
        if re.search(r"(time|நேரம்|மணி|slot)", lower):
            if "?" in response or "சரியா" in response:
                return "time"
        if re.search(r"(name|பேரு|பெயர்|நேம்)", lower):
            if "?" in response or "சொல்லுங்க" in response:
                return "name"
        return None
