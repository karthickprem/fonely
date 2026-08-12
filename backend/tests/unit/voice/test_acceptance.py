"""Tests for acceptance matrix contract completeness."""

from fonely.voice.acceptance import (
    ACCEPTANCE_MATRIX,
    ForbiddenBehavior,
    RequiredPort,
    TerminalOutcome,
)


def test_matrix_has_required_scenarios():
    ids = {s.id for s in ACCEPTANCE_MATRIX}
    assert len(ids) == len(ACCEPTANCE_MATRIX)
    assert len(ACCEPTANCE_MATRIX) >= 12


def test_all_terminal_outcomes_covered():
    covered = {s.terminal_outcome for s in ACCEPTANCE_MATRIX}
    required = {
        TerminalOutcome.INQUIRY_ANSWERED,
        TerminalOutcome.BOOKING_COMPLETED,
        TerminalOutcome.BOOKING_REFUSED_TEST_MODE,
        TerminalOutcome.HANDOFF,
        TerminalOutcome.ABANDONED,
        TerminalOutcome.CORRECTION_HANDLED,
        TerminalOutcome.UNAVAILABLE_SLOT,
        TerminalOutcome.CLOSED_DAY,
        TerminalOutcome.SAFETY_ESCALATION,
    }
    assert required <= covered


def test_all_forbidden_behaviors_referenced():
    used = set()
    for s in ACCEPTANCE_MATRIX:
        used |= s.forbidden_behaviors
    for behavior in ForbiddenBehavior:
        assert behavior in used, f"{behavior} not referenced in any scenario"


def test_max_turns_bounded():
    for s in ACCEPTANCE_MATRIX:
        assert 1 <= s.max_turns <= 15, f"{s.id} max_turns={s.max_turns}"


def test_booking_requires_validator_and_conversation():
    for s in ACCEPTANCE_MATRIX:
        if s.terminal_outcome == TerminalOutcome.BOOKING_COMPLETED:
            assert RequiredPort.VALIDATOR_PORT in s.required_ports
            assert RequiredPort.CONVERSATION_SERVICE in s.required_ports


def test_availability_scenarios_require_clock_and_port():
    availability_ids = {"AC-002", "AC-005", "AC-006", "AC-011"}
    for s in ACCEPTANCE_MATRIX:
        if s.id in availability_ids:
            assert RequiredPort.TRUSTED_CLOCK in s.required_ports
            assert RequiredPort.AVAILABILITY_QUERY in s.required_ports


def test_today_scenario_forbids_unresolved_date():
    s = next(s for s in ACCEPTANCE_MATRIX if s.id == "AC-002")
    assert ForbiddenBehavior.UNRESOLVED_RELATIVE_DATE in s.forbidden_behaviors
    assert ForbiddenBehavior.GENERIC_SCHEDULE_AS_AVAILABILITY in s.forbidden_behaviors
