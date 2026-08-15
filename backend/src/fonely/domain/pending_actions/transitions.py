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
        # Owner-reply-resume lifecycle (#43, action_type=awaiting_owner_reply).
        # AWAITING_OWNER_REPLY: call suspended, owner not yet replied. The owner
        # reply (separate process) advances it to RESUME_REQUESTED; a hangup
        # cancels it; the expiry sweep expires it (= timed_out).
        PendingActionStatus.AWAITING_OWNER_REPLY: frozenset(
            {
                PendingActionStatus.RESUME_REQUESTED,
                PendingActionStatus.CANCELLED,
                PendingActionStatus.EXPIRED,
            }
        ),
        # RESUME_REQUESTED: owner replied, answer persisted, not yet delivered.
        # The voice claim loop delivers it (RESUMED); or it expires if the owning
        # replica never claims (owner replied but the call already ended).
        PendingActionStatus.RESUME_REQUESTED: frozenset(
            {
                PendingActionStatus.RESUMED,
                PendingActionStatus.CANCELLED,
                PendingActionStatus.EXPIRED,
            }
        ),
        # RESUMED: the reconciled answer was spoken into the live call. Terminal.
        PendingActionStatus.RESUMED: frozenset(),
    }
)

TERMINAL_STATUSES = frozenset(
    {
        PendingActionStatus.CONFIRMED,
        PendingActionStatus.REJECTED,
        PendingActionStatus.CANCELLED,
        PendingActionStatus.EXPIRED,
        PendingActionStatus.RESUMED,
    }
)

EXPIRABLE_STATUSES = frozenset(
    {
        PendingActionStatus.COLLECTING_DETAILS,
        PendingActionStatus.AWAITING_CONFIRMATION,
        # A never-answered owner-reply wait ages to EXPIRED via the sweep — this
        # IS its timed_out. Mirrors bulk_expire's eligible tuple.
        PendingActionStatus.AWAITING_OWNER_REPLY,
        PendingActionStatus.RESUME_REQUESTED,
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
