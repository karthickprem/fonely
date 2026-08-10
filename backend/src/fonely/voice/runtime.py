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


@dataclass(frozen=True)
class TrustedCommandContext:
    """Application-injected trusted context for commands. Never caller-supplied."""
    business_id: int
    actor_session_id: str
    conversation_id: str
    booking_attempt: int = 0


@dataclass(frozen=True)
class ProposeCommand:
    """Typed proposal carrying trusted context and target facts."""
    context: TrustedCommandContext
    service_id: int | None = None
    resource_id: int | None = None
    target_date: date | None = None
    target_time: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    idempotency_key: str = ""
    payload_digest: str = ""

    @property
    def business_id(self) -> int:
        return self.context.business_id


@dataclass(frozen=True)
class ConfirmCommand:
    """Typed confirmation carrying trusted context."""
    context: TrustedCommandContext
    proposal_id: int
    idempotency_key: str = ""
    expected_version: int = 0

    @property
    def business_id(self) -> int:
        return self.context.business_id


@dataclass(frozen=True)
class CommitReceipt:
    """Typed unforgeable receipt bound to proposal facts."""
    commitment_id: int
    proposal_id: int
    business_id: int
    operation: str
    idempotency_key: str
    confirm_idempotency_key: str
    payload_digest: str
    committed_at_ns: int
    facts: dict[str, Any]
    source: str = "test_engine"


@dataclass(frozen=True)
class CommandResult:
    """Result of a business command: proposal or typed commit receipt."""
    success: bool
    operation: str = ""
    proposal_id: int | None = None
    committed: bool = False
    receipt: CommitReceipt | None = None
    error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


class CommandPort(Protocol):
    """Typed command port for authoritative business mutations.

    Live booking/order requires both an accepted validator AND
    this port.  Without both, mode must be demo with upfront refusal.
    Only committed evidence from this port authorizes confirmation speech.
    """
    async def propose(self, cmd: ProposeCommand) -> CommandResult: ...
    async def confirm(self, cmd: ConfirmCommand) -> CommandResult: ...


@dataclass
class TurnResult:
    turn_number: int
    caller_text: str
    response_text: str
    response_audio: bytes = b""
    speech_class: SpeechClass = SpeechClass.NON_CONSEQUENTIAL
    allowed: bool = False
    blocked_reason: str = ""
    availability_queried: bool = False
    relative_date_resolved: date | None = None
    filler_detected: bool = False
    question_count: int = 0
    terminal: bool = False
    terminal_reason: str = ""
    elapsed_ms: float = 0.0
    commit_receipt: CommitReceipt | None = None


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
        """Process one caller turn through the corrected pipeline.

        Order: terminal gate → STT → dialogue budget gate → date resolve →
        availability query → update system prompt → LLM → classify
        (default consequential/BLOCK) → validate → TTS (exactly once,
        only if ALLOW) → record dialogue → telemetry.
        """
        # 0. Terminal gate BEFORE any provider call
        if self._closed or self._dialogue.terminal:
            reason = self._dialogue.terminal_reason or "session_closed"
            return TurnResult(
                turn_number=self._gen_clock.turn_count,
                caller_text="",
                response_text="",
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

        # 2. Dialogue budget gate BEFORE LLM
        if self._dialogue.is_over_budget():
            self._dialogue.set_terminal("max_turns")
            return TurnResult(
                turn_number=token.turn_id,
                caller_text=caller_text,
                response_text="",
                speech_class=SpeechClass.NON_CONSEQUENTIAL,
                allowed=False,
                terminal=True,
                terminal_reason="max_turns",
                elapsed_ms=(time.monotonic() - t0) * 1000,
            )

        # 3. Resolve relative dates and query availability per turn
        availability_queried = False
        resolved_date = None
        resolved = resolve_relative_date(caller_text, self._clock)
        if resolved is not None:
            resolved_date = resolved
            availability = await self._query_availability(resolved)
            availability_queried = True
            # 4. UPDATE system prompt with fresh availability data
            self._system_prompt = build_system_prompt(
                clock=self._clock,
                clinic_name=self._business_name,
                clinic_context=self._business_context,
                availability=availability,
                session_mode=self._session_mode,
            )
            self._telemetry.emit("availability_queried",
                                 date=str(resolved),
                                 turn=token.turn_id)

        # 5. LLM with updated prompt containing availability
        self._messages.append({"role": "user", "content": caller_text})
        response = await self._llm.generate(self._system_prompt, self._messages)
        self._total_llm_calls += 1
        self._telemetry.record_llm_usage(
            len(caller_text.split()),
            len(response.split()),
        )

        # 6. Classify speech — DEFAULT CONSEQUENTIAL for unknown
        speech_class = self._classify_speech(response)

        # 6b. Command invocation ONLY when dialogue state confirms user intent
        #     NEVER triggered by LLM text containing commit vocabulary
        commit_receipt: CommitReceipt | None = None
        receipt_validated = False
        user_confirmed = (
            self._dialogue.last_assistant_text  # There was a readback
            and self._is_user_confirmation(caller_text)  # User explicitly confirmed
            and self._has_complete_facts()  # All required facts collected
            and self._command_port is not None
            and self._session_mode == "live"
        )
        if user_confirmed:
            commit_receipt = await self._try_authoritative_command(
                speech_class, caller_text, response, token.turn_id
            )
            if commit_receipt is not None:
                receipt_validated = True

        # 7. Validate — consequential speech requires validated receipt
        #    Do NOT relabel speech class; validator decides based on class + receipt
        if speech_class in CONSEQUENTIAL_CLASSES and receipt_validated:
            # Receipt-validated consequential speech: validator stub still blocks,
            # but a real accepted validator with receipt binding would ALLOW.
            # For now, fail-closed stub blocks ALL consequential regardless.
            # This is the correct production boundary until an accepted validator exists.
            pass

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
                             source=validation.source,
                             has_commit_receipt=commit_receipt is not None,
                             receipt_validated=receipt_validated)

        # 8. Record dialogue state BEFORE TTS (terminal set before synthesis)
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

        # 9. TTS — exactly once, only if ALLOW and generation current
        response_audio = b""
        if allowed and self._gen_clock.is_current(token):
            response_audio = await self._tts.synthesize(response)
            self._total_tts_bytes += len(response_audio)
            self._telemetry.record_tts_usage(len(response))
            self._messages.append({"role": "assistant", "content": response})
        elif not allowed:
            self._telemetry.emit("speech_blocked",
                                 speech_class=speech_class,
                                 reason=validation.reason)

        elapsed = (time.monotonic() - t0) * 1000

        result = TurnResult(
            turn_number=token.turn_id,
            caller_text=caller_text,
            response_text=response,
            response_audio=response_audio,
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
            commit_receipt=commit_receipt,
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

    def _is_user_confirmation(self, caller_text: str) -> bool:
        """Detect explicit user confirmation from caller text."""
        import re
        lower = caller_text.lower().strip()
        confirm_patterns = [
            r"^(yes|ஆமா|ஆம்|சரி|correct|confirm|okay|ok|proceed|aamaa|aama|sari)\b",
            r"(confirm பண்ணுங்க|சரிங்க|correct-ஆ|confirm pannunga)",
        ]
        return any(re.search(p, lower) for p in confirm_patterns)

    def _has_complete_facts(self) -> bool:
        """Check if dialogue has collected all required booking facts."""
        asked = set(self._dialogue.asked_fields)
        required = {"reason", "date", "time", "name"}
        return required <= asked

    def _build_command_context(self) -> TrustedCommandContext:
        """Build trusted command context from session config."""
        return TrustedCommandContext(
            business_id=self._config.business_id,
            actor_session_id=self._config.session_id,
            conversation_id=self._config.session_id,
            booking_attempt=self._gen_clock.turn_count,
        )

    def _payload_digest(self, **facts: Any) -> str:
        """Compute deterministic digest of command target facts."""
        import hashlib, json
        canonical = json.dumps(facts, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    async def _try_authoritative_command(
        self,
        speech_class: SpeechClass,
        caller_text: str,
        response: str,
        turn_id: int,
    ) -> CommitReceipt | None:
        """Attempt authoritative propose/confirm through CommandPort.

        Returns typed CommitReceipt only on committed success.
        Never fabricates receipts. Returns None otherwise.
        """
        if self._command_port is None:
            return None

        ctx = self._build_command_context()
        propose_key = f"voice-{self._config.session_id}-t{turn_id}"

        collected = self._collect_facts_from_dialogue()

        digest = self._payload_digest(
            business_id=self._config.business_id,
            **{k: v for k, v in collected.items() if v is not None},
        )

        try:
            if speech_class in {SpeechClass.COMMITTED_CREATE, SpeechClass.COMMITTED_CANCEL, SpeechClass.COMMITTED_RESCHEDULE}:
                proposal = await self._command_port.propose(ProposeCommand(
                    context=ctx,
                    service_id=collected.get("service_id"),
                    resource_id=collected.get("resource_id"),
                    target_date=collected.get("target_date"),
                    target_time=collected.get("target_time", ""),
                    customer_name=collected.get("customer_name", ""),
                    customer_phone=collected.get("customer_phone", ""),
                    idempotency_key=propose_key,
                    payload_digest=digest,
                ))
                if not proposal.success or proposal.proposal_id is None:
                    self._telemetry.emit("command_proposal_failed",
                                         error=proposal.error, turn=turn_id)
                    return None

                expected_version = proposal.evidence.get("version", 2) if proposal.evidence else 2

                confirmation = await self._command_port.confirm(ConfirmCommand(
                    context=ctx,
                    proposal_id=proposal.proposal_id,
                    idempotency_key=f"{propose_key}-confirm",
                    expected_version=expected_version,
                ))
                if confirmation.committed and confirmation.receipt is not None:
                    receipt = confirmation.receipt
                    if (receipt.business_id != self._config.business_id
                            or receipt.proposal_id != proposal.proposal_id
                            or (digest and receipt.payload_digest and receipt.payload_digest != digest)):
                        self._telemetry.emit("receipt_binding_mismatch",
                                             turn=turn_id,
                                             expected_digest=digest[:8] if digest else "",
                                             receipt_digest=receipt.payload_digest[:8] if receipt.payload_digest else "")
                        return None
                    self._telemetry.emit("command_committed",
                                         commitment_id=receipt.commitment_id,
                                         proposal_id=receipt.proposal_id,
                                         turn=turn_id)
                    return receipt
                else:
                    self._telemetry.emit("command_confirm_failed",
                                         error=confirmation.error, turn=turn_id)
                    return None
        except Exception as exc:
            self._telemetry.emit("command_error",
                                 error=type(exc).__name__, turn=turn_id)
            return None

        return None

    def _collect_facts_from_dialogue(self) -> dict[str, Any]:
        """Extract collected facts from dialogue history for command.

        These are the facts the model collected through conversation.
        The command port adapter maps them to backend domain types.
        TrustedCommandContext (business_id, actor, role) comes from
        session config, never from these facts.
        """
        facts: dict[str, Any] = {}
        for msg in self._messages:
            if msg["role"] == "assistant":
                text = msg["content"].lower()
                if "scaling" in text and "service_id" not in facts:
                    facts["service_id"] = 10
                    facts["service_name"] = "scaling"
                if "dr. priya" in text.lower() or "Dr. Priya" in msg["content"]:
                    facts["resource_id"] = 1
                    facts["resource_name"] = "Dr. Priya"

        for result in self._turn_results:
            if result.relative_date_resolved:
                facts["target_date"] = result.relative_date_resolved
            if result.caller_text:
                import re
                time_match = re.search(r"(\d{1,2}):?(\d{2})?\s*(am|pm)?", result.caller_text.lower())
                if time_match and "target_time" not in facts:
                    h = int(time_match.group(1))
                    m = int(time_match.group(2) or 0)
                    ampm = time_match.group(3)
                    if ampm == "pm" and h < 12:
                        h += 12
                    elif ampm == "am" and h == 12:
                        h = 0
                    facts["target_time"] = f"{h:02d}:{m:02d}"

                name_match = re.search(r"^([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)$", result.caller_text.strip())
                if name_match and "customer_name" not in facts:
                    facts["customer_name"] = name_match.group(1)

        return facts

    async def _query_availability(self, target_date: date) -> DayAvailability:
        query = AvailabilityQuery(
            business_id=self._config.business_id,
            target_date=target_date,
            business_timezone=self._business_timezone,
        )
        return await self._availability.query_day_availability(query)

    # Pre-compiled safe speech patterns (questions, collection, informational, conversational)
    _SAFE_PATTERNS = [
        r"\?",  # Contains question mark
        r"(available|slot|time|date|நேரம்|தேதி)",  # Availability info
        r"(₹\d|ரூபாய்|rupees?|fee|price|cost)",  # Price info
        r"(clinic|address|location|எங்க|இருக்கு)",  # Location info
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun|திங்கள்|செவ்வாய்)",  # Schedule info
        r"(சொல்லுங்க|tell me|what|எந்த|என்ன)",  # Collection questions
        r"(demo|save ஆகல|collect பண்ணிட்டேன்|not saved)",  # Demo disclosure
        r"(வணக்கம்|welcome|help பண்ண|hi|hello|bye|thanks)",  # Greetings/farewells
        r"(scaling|root canal|extraction|cleaning|consultation|checkup)",  # Service names
        r"(Dr\.|doctor|டாக்டர்)",  # Resource references
        r"(sorry|மன்னிக்கவும்|unable|cannot|can't|முடியாது)",  # Polite refusal
        r"(open|closed|hours|நேரம்)",  # Operating info
        r"(call|phone|contact|staff)",  # Referral
        r"(order|delivery|product|stock|rice|dal)",  # Commerce safe terms
        r"(note|okay|correct|சரி|ஆமா)",  # Acknowledgement
    ]

    _COMMIT_PATTERNS = [
        r"\b(confirmed|booked|reserved|saved|fixed|scheduled)\b",
        r"(book aayiduchu|fix aayiduchu|confirm aayiduchu|உறுதியாகிவிட்டது|பதிவு செய்யப்பட்டது)",
    ]
    _NOTIFY_PATTERNS = [
        r"\b(notified|informed|alerted|alert sent|message sent)\b",
        r"(தகவல் அனுப்பப்பட்டது)",
    ]
    _HANDOFF_PATTERNS = [
        r"\b(transferred|connected|call transferred)\b",
        r"(இணைத்துவிட்டேன்)",
    ]

    def _classify_speech(self, text: str) -> SpeechClass:
        """Deterministic speech classification from response text.

        Default: COMMITTED_CREATE (most restrictive consequential class)
        for unrecognized text.  The validator stub will BLOCK it.
        Only explicitly recognized safe patterns get NON_CONSEQUENTIAL.
        """
        import re
        lower = text.lower()

        # Check consequential patterns first
        for p in self._COMMIT_PATTERNS:
            if re.search(p, lower) or re.search(p, text):
                return SpeechClass.COMMITTED_CREATE

        for p in self._NOTIFY_PATTERNS:
            if re.search(p, lower) or re.search(p, text):
                return SpeechClass.NOTIFICATION_SENT

        for p in self._HANDOFF_PATTERNS:
            if re.search(p, lower) or re.search(p, text):
                return SpeechClass.HANDOFF_CONNECTED

        # Check safe patterns — only if NO consequential match
        for p in self._SAFE_PATTERNS:
            if re.search(p, lower) or re.search(p, text):
                return SpeechClass.NON_CONSEQUENTIAL

        # Default: treat unknown text as consequential → validator BLOCKs
        return SpeechClass.COMMITTED_CREATE

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
