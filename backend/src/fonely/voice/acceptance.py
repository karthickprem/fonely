"""Voice conversation acceptance matrix and defect taxonomy.

Defines the executable acceptance contract for production voice
conversations.  Each acceptance scenario specifies terminal outcome,
maximum turns, required typed port usage, forbidden behaviors, and
grading criteria.

Root-cause postmortem for R&D live transcript defects:

1. AWKWARD_NONRESPONSIVE: "சோ அதனால பாக்கணும்" — LLM generated
   filler/narration instead of answering.  Root cause: prompt lacks
   strict response-discipline enforcement for "answer or ask, never
   narrate."  Fix: response discipline is prompt architecture, not
   a runtime patch.  Runtime contribution: turn-count cap and
   repetition detector in dialogue state.

2. TODAY_UNRESOLVED: "இன்னைக்கு எந்த day-ன்னு தெரியல" — LLM could
   not resolve "today" because no trusted datetime was injected.
   Root cause: no TrustedClock in session context.
   Fix: voice/context.py TrustedClock + resolve_relative_date().

3. GENERIC_SCHEDULE_AS_AVAILABILITY: Recited Mon-Sat hours instead
   of actual doctor availability.  Root cause: static prompt text
   contained operating hours but no availability query port.
   Fix: voice/context.py AvailabilityPort distinguishes operating
   hours from available slots.

4. HARDCODED_TOMORROW_SLOTS: "Tomorrow: 10, 11, 5, 6:30, 7:30"
   in prompt masqueraded as live availability.
   Root cause: no authoritative availability query.
   Fix: AvailabilityPort returns typed DayAvailability from backend.

5. UNSOLICITED_STEERING: Redirected user from today to tomorrow
   without checking today's availability first.
   Root cause: static prompt assumed tomorrow; no authoritative
   today query.  Fix: resolve_relative_date + AvailabilityPort
   for the requested date before suggesting alternatives.

6. NO_ACTUAL_BOOKING: Collected all details but never created a
   proposal or booking.  Root cause: R&D lab had no backend
   integration; demo-mode prompt prevented commitment.
   Fix: production runtime uses ConversationService via typed
   command port (future milestone, after validator acceptance).

7. LAB_LIMITATION_LATE: Disclosed "details collected but not saved"
   only after full collection flow.  Root cause: demo-mode was
   embedded in prompt, not in typed session configuration.
   Fix: session config carries explicit mode (demo/shadow/live)
   that controls whether booking commands are available, disclosed
   upfront rather than after collection.

8. PROLONGED_IMPOSSIBLE_GOAL: Continued collecting for an
   impossible booking through many turns.
   Root cause: no turn budget or futility detection.
   Fix: SessionLimits.max_turns + dialogue state tracks
   collection futility (e.g., no available slots for requested
   date/resource).

9. WEAK_CLOSURE: No clean terminal behavior after demo completion.
   Root cause: prompt said "stop" but LLM continued.
   Fix: terminal state in dialogue state machine produces
   deterministic closure response, not LLM-generated.

10. STATIC_FACTS_AS_MUTABLE: Hardcoded prices, doctors, hours in
    prompt presented as current state.
    Root cause: no typed business context port.
    Fix: production runtime injects only authoritative facts
    from backend; prompt contains only immutable behavioral
    instructions, not clinic data.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class TerminalOutcome(enum.StrEnum):
    INQUIRY_ANSWERED = "inquiry_answered"
    BOOKING_COMPLETED = "booking_completed"
    BOOKING_REFUSED_TEST_MODE = "booking_refused_test_mode"
    HANDOFF = "handoff"
    ABANDONED = "abandoned"
    CORRECTION_HANDLED = "correction_handled"
    UNAVAILABLE_SLOT = "unavailable_slot"
    CLOSED_DAY = "closed_day"
    SAFETY_ESCALATION = "safety_escalation"
    MAX_TURNS_REACHED = "max_turns_reached"
    SESSION_TIMEOUT = "session_timeout"


class ForbiddenBehavior(enum.StrEnum):
    NARRATION_FILLER = "narration_filler"
    UNRESOLVED_RELATIVE_DATE = "unresolved_relative_date"
    GENERIC_SCHEDULE_AS_AVAILABILITY = "generic_schedule_as_availability"
    HARDCODED_SLOTS = "hardcoded_slots"
    UNSOLICITED_DATE_STEERING = "unsolicited_date_steering"
    FALSE_BOOKING_CLAIM = "false_booking_claim"
    LATE_MODE_DISCLOSURE = "late_mode_disclosure"
    PROLONGED_IMPOSSIBLE_GOAL = "prolonged_impossible_goal"
    REPEATED_QUESTION = "repeated_question"
    STATIC_FACTS_AS_MUTABLE = "static_facts_as_mutable"
    CONSEQUENTIAL_WITHOUT_EVIDENCE = "consequential_without_evidence"


class RequiredPort(enum.StrEnum):
    TRUSTED_CLOCK = "trusted_clock"
    AVAILABILITY_QUERY = "availability_query"
    BUSINESS_CONTEXT = "business_context"
    VALIDATOR_PORT = "validator_port"
    CONVERSATION_SERVICE = "conversation_service"


@dataclass(frozen=True)
class AcceptanceScenario:
    id: str
    name: str
    terminal_outcome: TerminalOutcome
    max_turns: int
    required_ports: frozenset[RequiredPort]
    forbidden_behaviors: frozenset[ForbiddenBehavior]
    requires_live_audio: bool = False
    requires_native_grading: bool = False
    description: str = ""


ACCEPTANCE_MATRIX: tuple[AcceptanceScenario, ...] = (
    AcceptanceScenario(
        id="AC-001",
        name="simple_inquiry_answered",
        terminal_outcome=TerminalOutcome.INQUIRY_ANSWERED,
        max_turns=4,
        required_ports=frozenset({RequiredPort.TRUSTED_CLOCK, RequiredPort.BUSINESS_CONTEXT}),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.NARRATION_FILLER,
            ForbiddenBehavior.HARDCODED_SLOTS,
            ForbiddenBehavior.STATIC_FACTS_AS_MUTABLE,
        }),
        description="User asks clinic hours or fee; agent answers from authoritative context, no booking offer.",
    ),
    AcceptanceScenario(
        id="AC-002",
        name="today_availability_check",
        terminal_outcome=TerminalOutcome.INQUIRY_ANSWERED,
        max_turns=4,
        required_ports=frozenset({
            RequiredPort.TRUSTED_CLOCK,
            RequiredPort.AVAILABILITY_QUERY,
        }),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.UNRESOLVED_RELATIVE_DATE,
            ForbiddenBehavior.GENERIC_SCHEDULE_AS_AVAILABILITY,
            ForbiddenBehavior.UNSOLICITED_DATE_STEERING,
        }),
        description="User asks if doctor is free today; agent resolves today via trusted clock, queries availability port, reports actual slots or no availability.",
    ),
    AcceptanceScenario(
        id="AC-003",
        name="booking_completed_test_engine",
        terminal_outcome=TerminalOutcome.BOOKING_COMPLETED,
        max_turns=10,
        required_ports=frozenset({
            RequiredPort.TRUSTED_CLOCK,
            RequiredPort.AVAILABILITY_QUERY,
            RequiredPort.VALIDATOR_PORT,
            RequiredPort.CONVERSATION_SERVICE,
        }),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.FALSE_BOOKING_CLAIM,
            ForbiddenBehavior.CONSEQUENTIAL_WITHOUT_EVIDENCE,
            ForbiddenBehavior.PROLONGED_IMPOSSIBLE_GOAL,
            ForbiddenBehavior.REPEATED_QUESTION,
        }),
        requires_live_audio=True,
        requires_native_grading=True,
        description="Full booking flow against test authoritative engine: collect→propose→confirm→commit with evidence.",
    ),
    AcceptanceScenario(
        id="AC-004",
        name="test_mode_refusal_upfront",
        terminal_outcome=TerminalOutcome.BOOKING_REFUSED_TEST_MODE,
        max_turns=3,
        required_ports=frozenset({RequiredPort.TRUSTED_CLOCK}),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.LATE_MODE_DISCLOSURE,
            ForbiddenBehavior.PROLONGED_IMPOSSIBLE_GOAL,
        }),
        description="In demo/test mode, agent discloses limitation before collecting booking details, not after.",
    ),
    AcceptanceScenario(
        id="AC-005",
        name="unavailable_slot_alternatives",
        terminal_outcome=TerminalOutcome.UNAVAILABLE_SLOT,
        max_turns=6,
        required_ports=frozenset({
            RequiredPort.TRUSTED_CLOCK,
            RequiredPort.AVAILABILITY_QUERY,
        }),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.HARDCODED_SLOTS,
            ForbiddenBehavior.UNSOLICITED_DATE_STEERING,
        }),
        description="Requested slot unavailable; agent offers authoritative alternatives from availability port.",
    ),
    AcceptanceScenario(
        id="AC-006",
        name="closed_day_handling",
        terminal_outcome=TerminalOutcome.CLOSED_DAY,
        max_turns=4,
        required_ports=frozenset({
            RequiredPort.TRUSTED_CLOCK,
            RequiredPort.AVAILABILITY_QUERY,
        }),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.GENERIC_SCHEDULE_AS_AVAILABILITY,
        }),
        description="User requests appointment on closed day (Sunday or holiday); agent reports closed from availability port.",
    ),
    AcceptanceScenario(
        id="AC-007",
        name="correction_handled",
        terminal_outcome=TerminalOutcome.CORRECTION_HANDLED,
        max_turns=8,
        required_ports=frozenset({RequiredPort.TRUSTED_CLOCK}),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.REPEATED_QUESTION,
            ForbiddenBehavior.NARRATION_FILLER,
        }),
        description="User corrects name/date/time during booking; agent updates without re-asking already-provided fields.",
    ),
    AcceptanceScenario(
        id="AC-008",
        name="abandon_clean_closure",
        terminal_outcome=TerminalOutcome.ABANDONED,
        max_turns=3,
        required_ports=frozenset(),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.PROLONGED_IMPOSSIBLE_GOAL,
        }),
        description="User abandons booking; agent acknowledges once and stops prompting.",
    ),
    AcceptanceScenario(
        id="AC-009",
        name="safety_escalation",
        terminal_outcome=TerminalOutcome.SAFETY_ESCALATION,
        max_turns=2,
        required_ports=frozenset(),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.CONSEQUENTIAL_WITHOUT_EVIDENCE,
        }),
        description="User describes emergency; deterministic safety response, no booking continuation.",
    ),
    AcceptanceScenario(
        id="AC-010",
        name="repeated_question_no_repetition",
        terminal_outcome=TerminalOutcome.INQUIRY_ANSWERED,
        max_turns=4,
        required_ports=frozenset({RequiredPort.TRUSTED_CLOCK}),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.REPEATED_QUESTION,
            ForbiddenBehavior.NARRATION_FILLER,
        }),
        description="User asks the same question twice; agent answers consistently without filler or confusion.",
    ),
    AcceptanceScenario(
        id="AC-011",
        name="timezone_day_boundary",
        terminal_outcome=TerminalOutcome.INQUIRY_ANSWERED,
        max_turns=4,
        required_ports=frozenset({
            RequiredPort.TRUSTED_CLOCK,
            RequiredPort.AVAILABILITY_QUERY,
        }),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.UNRESOLVED_RELATIVE_DATE,
        }),
        description="Call at IST 23:30; 'today' resolves correctly to the IST date, not UTC next day.",
    ),
    AcceptanceScenario(
        id="AC-012",
        name="handoff_to_staff",
        terminal_outcome=TerminalOutcome.HANDOFF,
        max_turns=4,
        required_ports=frozenset(),
        forbidden_behaviors=frozenset({
            ForbiddenBehavior.PROLONGED_IMPOSSIBLE_GOAL,
        }),
        description="Request exceeds automated capability; agent hands off cleanly without false promises.",
    ),
)
