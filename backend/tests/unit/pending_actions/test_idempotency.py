"""Pure service idempotency-equivalence tests."""

from datetime import UTC, datetime, timedelta

import pytest

from fonely.domain.pending_actions.errors import PendingActionIdempotencyConflictError
from fonely.models.enums import PendingActionType
from fonely.models.schema import PendingAction
from fonely.services.pending_actions import PendingActionService

EXPIRY = datetime(2026, 8, 1, 8, 15, tzinfo=UTC)


def existing_action() -> PendingAction:
    return PendingAction(
        action_type="order",
        payload_schema_version=1,
        payload_digest="a" * 64,
        expires_at=EXPIRY,
        session_id="session-1",
    )


def assert_equivalent(**overrides: object) -> None:
    values: dict[str, object] = {
        "action_type": PendingActionType.ORDER,
        "schema_version": 1,
        "digest": "a" * 64,
        "expires_at": EXPIRY,
        "session_id": "session-1",
    }
    values.update(overrides)
    PendingActionService._assert_idempotent_equivalence(
        existing_action(),
        **values,  # type: ignore[arg-type]
    )


def test_equivalent_request_is_accepted() -> None:
    assert_equivalent()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action_type", PendingActionType.OWNER_STOCK_UPDATE),
        ("schema_version", 2),
        ("digest", "b" * 64),
        ("expires_at", EXPIRY + timedelta(minutes=1)),
        ("session_id", "session-2"),
    ],
)
def test_material_request_semantics_conflict(field: str, value: object) -> None:
    with pytest.raises(PendingActionIdempotencyConflictError):
        assert_equivalent(**{field: value})
