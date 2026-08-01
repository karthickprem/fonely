"""Pure lifecycle mutation tests."""

from datetime import UTC, datetime, timedelta

import pytest

from fonely.domain.pending_actions.errors import (
    InvalidStateTransitionError,
    PendingActionExpiredError,
)
from fonely.domain.pending_actions.lifecycle import (
    PendingActionState,
    awaiting_confirmation_state,
    begin_commit_state,
    complete_commit_state,
    expire_state,
    fail_commit_state,
    revise_state,
)
from fonely.models.enums import PendingActionStatus

NOW = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)


def state(status: PendingActionStatus, **overrides: object) -> PendingActionState:
    values: dict[str, object] = {
        "status": status,
        "version": 1,
        "expires_at": NOW + timedelta(minutes=15),
    }
    values.update(overrides)
    return PendingActionState(**values)  # type: ignore[arg-type]


def test_revision_from_awaiting_clears_confirmation_and_commit_metadata() -> None:
    revised = revise_state(
        state(
            PendingActionStatus.AWAITING_CONFIRMATION,
            confirmation_snapshot="snapshot",
            confirmed_by="+919123456789",
            confirmed_at=NOW,
            committed_entity_type="order",
            committed_entity_id=4,
            commit_error_code="old_error",
            commit_error_message="old message",
            rejection_reason_code="old_reason",
        )
    )
    assert revised.status == PendingActionStatus.COLLECTING_DETAILS
    assert revised.version == 2
    assert revised.confirmation_snapshot is None
    assert revised.confirmed_by is None
    assert revised.confirmed_at is None
    assert revised.committed_entity_type is None
    assert revised.committed_entity_id is None
    assert revised.commit_error_code is None
    assert revised.commit_error_message is None
    assert revised.rejection_reason_code is None


def test_revision_requires_new_confirmation() -> None:
    revised = revise_state(
        state(
            PendingActionStatus.AWAITING_CONFIRMATION,
            confirmation_snapshot="snapshot",
        )
    )
    with pytest.raises(InvalidStateTransitionError):
        begin_commit_state(revised, NOW)


def test_awaiting_confirmation_sets_snapshot() -> None:
    result = awaiting_confirmation_state(
        state(PendingActionStatus.COLLECTING_DETAILS),
        "canonical-snapshot",
        NOW,
    )
    assert result.status == PendingActionStatus.AWAITING_CONFIRMATION
    assert result.confirmation_snapshot == "canonical-snapshot"
    assert result.version == 2


def test_begin_commit_requires_snapshot() -> None:
    with pytest.raises(ValueError, match="Confirmation snapshot"):
        begin_commit_state(state(PendingActionStatus.AWAITING_CONFIRMATION), NOW)


def test_begin_commit_clears_old_errors() -> None:
    result = begin_commit_state(
        state(
            PendingActionStatus.AWAITING_CONFIRMATION,
            confirmation_snapshot="snapshot",
            commit_error_code="retryable",
            commit_error_message="try again",
        ),
        NOW,
    )
    assert result.status == PendingActionStatus.COMMITTING
    assert result.commit_error_code is None
    assert result.commit_error_message is None


def test_complete_commit_records_authoritative_entity() -> None:
    result = complete_commit_state(
        state(PendingActionStatus.COMMITTING, confirmation_snapshot="snapshot"),
        confirmed_by="+919123456789",
        confirmed_at=NOW,
        entity_type="order",
        entity_id=42,
    )
    assert result.status == PendingActionStatus.CONFIRMED
    assert result.committed_entity_type == "order"
    assert result.committed_entity_id == 42
    assert result.confirmed_by == "+919123456789"
    assert result.confirmed_at == NOW


def test_retryable_failure_returns_to_awaiting_confirmation() -> None:
    result = fail_commit_state(
        state(PendingActionStatus.COMMITTING, confirmation_snapshot="snapshot"),
        error_code="temporary_conflict",
        safe_message="Please confirm again",
        retryable=True,
    )
    assert result.status == PendingActionStatus.AWAITING_CONFIRMATION
    assert result.confirmation_snapshot == "snapshot"
    assert result.rejection_reason_code is None


def test_non_retryable_failure_rejects() -> None:
    result = fail_commit_state(
        state(PendingActionStatus.COMMITTING, confirmation_snapshot="snapshot"),
        error_code="invalid_product",
        safe_message="The product is unavailable",
        retryable=False,
    )
    assert result.status == PendingActionStatus.REJECTED
    assert result.rejection_reason_code == "invalid_product"


def test_exact_expiry_boundary_expires() -> None:
    result = expire_state(
        state(PendingActionStatus.COLLECTING_DETAILS, expires_at=NOW),
        NOW,
    )
    assert result.status == PendingActionStatus.EXPIRED


def test_future_expiry_cannot_expire() -> None:
    with pytest.raises(PendingActionExpiredError, match="not reached"):
        expire_state(state(PendingActionStatus.COLLECTING_DETAILS), NOW)


def test_expiry_is_idempotent() -> None:
    expired = state(PendingActionStatus.EXPIRED, expires_at=NOW)
    assert expire_state(expired, NOW) is expired


def test_committing_does_not_auto_expire() -> None:
    with pytest.raises(InvalidStateTransitionError):
        expire_state(
            state(PendingActionStatus.COMMITTING, expires_at=NOW),
            NOW,
        )
