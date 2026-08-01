"""Exhaustive tests for the pure PendingAction transition policy."""

import pytest

from fonely.domain.pending_actions.errors import InvalidStateTransitionError
from fonely.domain.pending_actions.transitions import (
    TERMINAL_STATUSES,
    allowed_targets,
    assert_transition_allowed,
)
from fonely.models.enums import PendingActionStatus

EXPECTED = {
    PendingActionStatus.COLLECTING_DETAILS: {
        PendingActionStatus.AWAITING_CONFIRMATION,
        PendingActionStatus.CANCELLED,
        PendingActionStatus.EXPIRED,
    },
    PendingActionStatus.AWAITING_CONFIRMATION: {
        PendingActionStatus.COLLECTING_DETAILS,
        PendingActionStatus.COMMITTING,
        PendingActionStatus.REJECTED,
        PendingActionStatus.CANCELLED,
        PendingActionStatus.EXPIRED,
    },
    PendingActionStatus.COMMITTING: {
        PendingActionStatus.CONFIRMED,
        PendingActionStatus.AWAITING_CONFIRMATION,
        PendingActionStatus.REJECTED,
    },
    PendingActionStatus.CONFIRMED: set(),
    PendingActionStatus.REJECTED: set(),
    PendingActionStatus.CANCELLED: set(),
    PendingActionStatus.EXPIRED: set(),
}


@pytest.mark.parametrize("source", list(PendingActionStatus))
def test_allowed_targets_exactly_match_policy(source: PendingActionStatus) -> None:
    assert allowed_targets(source) == frozenset(EXPECTED[source])


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source in PendingActionStatus
        for target in PendingActionStatus
        if target in EXPECTED[source]
    ],
)
def test_every_allowed_transition_succeeds(
    source: PendingActionStatus,
    target: PendingActionStatus,
) -> None:
    assert_transition_allowed(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source in PendingActionStatus
        for target in PendingActionStatus
        if target not in EXPECTED[source]
    ],
)
def test_every_forbidden_transition_raises(
    source: PendingActionStatus,
    target: PendingActionStatus,
) -> None:
    with pytest.raises(InvalidStateTransitionError) as exc:
        assert_transition_allowed(source, target)
    assert exc.value.current == source
    assert exc.value.requested == target
    assert exc.value.code == "invalid_state_transition"


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_terminal_states_have_no_targets(status: PendingActionStatus) -> None:
    assert allowed_targets(status) == frozenset()
