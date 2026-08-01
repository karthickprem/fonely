"""PendingAction creation expiry policy tests."""

from datetime import UTC, datetime, timedelta

import pytest

from fonely.domain.pending_actions.errors import PendingActionExpiredError
from fonely.services.pending_actions import MAX_EXPIRY_HORIZON, PendingActionService

NOW = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)


def test_future_expiry_accepted() -> None:
    PendingActionService._validate_expiry(NOW + timedelta(minutes=15), NOW)


def test_past_expiry_rejected() -> None:
    with pytest.raises(PendingActionExpiredError):
        PendingActionService._validate_expiry(NOW - timedelta(seconds=1), NOW)


def test_equal_expiry_rejected() -> None:
    with pytest.raises(PendingActionExpiredError):
        PendingActionService._validate_expiry(NOW, NOW)


def test_exact_maximum_horizon_accepted() -> None:
    PendingActionService._validate_expiry(NOW + MAX_EXPIRY_HORIZON, NOW)


def test_excessive_horizon_rejected() -> None:
    with pytest.raises(ValueError, match="maximum"):
        PendingActionService._validate_expiry(
            NOW + MAX_EXPIRY_HORIZON + timedelta(microseconds=1),
            NOW,
        )


def test_naive_expiry_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PendingActionService._validate_expiry(datetime(2026, 8, 1, 8, 15), NOW)
