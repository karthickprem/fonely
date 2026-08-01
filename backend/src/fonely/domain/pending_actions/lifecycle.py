"""Pure PendingAction lifecycle mutations used by the application service."""

from dataclasses import dataclass, replace
from datetime import datetime

from fonely.domain.pending_actions.errors import PendingActionExpiredError
from fonely.domain.pending_actions.transitions import (
    assert_revision_allowed,
    assert_transition_allowed,
)
from fonely.models.enums import PendingActionStatus


@dataclass(frozen=True, slots=True)
class PendingActionState:
    status: PendingActionStatus
    version: int
    expires_at: datetime
    confirmation_snapshot: str | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    committed_entity_type: str | None = None
    committed_entity_id: int | None = None
    commit_error_code: str | None = None
    commit_error_message: str | None = None
    rejection_reason_code: str | None = None


def assert_not_expired(state: PendingActionState, now: datetime) -> None:
    if state.expires_at <= now:
        raise PendingActionExpiredError("Pending action has expired")


def revise_state(state: PendingActionState) -> PendingActionState:
    assert_revision_allowed(state.status)
    return replace(
        state,
        status=PendingActionStatus.COLLECTING_DETAILS,
        version=state.version + 1,
        confirmation_snapshot=None,
        confirmed_by=None,
        confirmed_at=None,
        committed_entity_type=None,
        committed_entity_id=None,
        commit_error_code=None,
        commit_error_message=None,
        rejection_reason_code=None,
    )


def awaiting_confirmation_state(
    state: PendingActionState,
    snapshot: str,
    now: datetime,
) -> PendingActionState:
    assert_not_expired(state, now)
    assert_transition_allowed(state.status, PendingActionStatus.AWAITING_CONFIRMATION)
    return replace(
        state,
        status=PendingActionStatus.AWAITING_CONFIRMATION,
        version=state.version + 1,
        confirmation_snapshot=snapshot,
    )


def begin_commit_state(state: PendingActionState, now: datetime) -> PendingActionState:
    assert_not_expired(state, now)
    assert_transition_allowed(state.status, PendingActionStatus.COMMITTING)
    if not state.confirmation_snapshot:
        raise ValueError("Confirmation snapshot is required before commit")
    return replace(
        state,
        status=PendingActionStatus.COMMITTING,
        version=state.version + 1,
        commit_error_code=None,
        commit_error_message=None,
    )


def complete_commit_state(
    state: PendingActionState,
    *,
    confirmed_by: str,
    confirmed_at: datetime,
    entity_type: str,
    entity_id: int,
) -> PendingActionState:
    assert_transition_allowed(state.status, PendingActionStatus.CONFIRMED)
    return replace(
        state,
        status=PendingActionStatus.CONFIRMED,
        version=state.version + 1,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        committed_entity_type=entity_type,
        committed_entity_id=entity_id,
        commit_error_code=None,
        commit_error_message=None,
    )


def fail_commit_state(
    state: PendingActionState,
    *,
    error_code: str,
    safe_message: str,
    retryable: bool,
) -> PendingActionState:
    target = (
        PendingActionStatus.AWAITING_CONFIRMATION if retryable else PendingActionStatus.REJECTED
    )
    assert_transition_allowed(state.status, target)
    return replace(
        state,
        status=target,
        version=state.version + 1,
        commit_error_code=error_code,
        commit_error_message=safe_message,
        rejection_reason_code=None if retryable else error_code,
    )


def expire_state(state: PendingActionState, now: datetime) -> PendingActionState:
    if state.status == PendingActionStatus.EXPIRED:
        return state
    if state.expires_at > now:
        raise PendingActionExpiredError("Action has not reached its expiry time")
    assert_transition_allowed(state.status, PendingActionStatus.EXPIRED)
    return replace(
        state,
        status=PendingActionStatus.EXPIRED,
        version=state.version + 1,
    )
