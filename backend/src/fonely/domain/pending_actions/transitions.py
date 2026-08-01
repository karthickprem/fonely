"""Pure PendingAction transition policy."""

from types import MappingProxyType

from fonely.domain.pending_actions.errors import InvalidStateTransitionError
from fonely.models.enums import PendingActionStatus

_ALLOWED_TRANSITIONS = MappingProxyType(
    {
        PendingActionStatus.COLLECTING_DETAILS: frozenset(
            {
                PendingActionStatus.AWAITING_CONFIRMATION,
                PendingActionStatus.CANCELLED,
                PendingActionStatus.EXPIRED,
            }
        ),
        PendingActionStatus.AWAITING_CONFIRMATION: frozenset(
            {
                PendingActionStatus.COLLECTING_DETAILS,
                PendingActionStatus.COMMITTING,
                PendingActionStatus.REJECTED,
                PendingActionStatus.CANCELLED,
                PendingActionStatus.EXPIRED,
            }
        ),
        PendingActionStatus.COMMITTING: frozenset(
            {
                PendingActionStatus.CONFIRMED,
                PendingActionStatus.AWAITING_CONFIRMATION,
                PendingActionStatus.REJECTED,
            }
        ),
        PendingActionStatus.CONFIRMED: frozenset(),
        PendingActionStatus.REJECTED: frozenset(),
        PendingActionStatus.CANCELLED: frozenset(),
        PendingActionStatus.EXPIRED: frozenset(),
    }
)

TERMINAL_STATUSES = frozenset(
    {
        PendingActionStatus.CONFIRMED,
        PendingActionStatus.REJECTED,
        PendingActionStatus.CANCELLED,
        PendingActionStatus.EXPIRED,
    }
)

EXPIRABLE_STATUSES = frozenset(
    {
        PendingActionStatus.COLLECTING_DETAILS,
        PendingActionStatus.AWAITING_CONFIRMATION,
    }
)


def allowed_targets(status: PendingActionStatus) -> frozenset[PendingActionStatus]:
    return _ALLOWED_TRANSITIONS[status]


def assert_transition_allowed(
    current: PendingActionStatus,
    requested: PendingActionStatus,
) -> None:
    if requested not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransitionError(current, requested)


def assert_revision_allowed(current: PendingActionStatus) -> None:
    """Revision is allowed while collecting or awaiting confirmation."""
    if current not in {
        PendingActionStatus.COLLECTING_DETAILS,
        PendingActionStatus.AWAITING_CONFIRMATION,
    }:
        raise InvalidStateTransitionError(current, PendingActionStatus.COLLECTING_DETAILS)
