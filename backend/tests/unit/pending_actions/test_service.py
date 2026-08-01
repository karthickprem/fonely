"""PendingAction service orchestration tests using an in-memory repository seam."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from fonely.domain.pending_actions.commands import (
    ActorContext,
    BeginCommitCommand,
    CancelPendingActionCommand,
    CommitResultContext,
    CompleteCommitCommand,
    ExpirePendingActionCommand,
    FailCommitCommand,
    MarkAwaitingConfirmationCommand,
    RevisePendingActionCommand,
)
from fonely.domain.pending_actions.errors import (
    CommitEntityConflictError,
    InvalidStateTransitionError,
    PendingActionConcurrencyError,
)
from fonely.domain.pending_actions.payloads import validate_payload
from fonely.domain.pending_actions.snapshots import (
    canonical_payload_dict,
    confirmation_snapshot,
    payload_digest,
)
from fonely.models.enums import CallerRole, PendingActionStatus, PendingActionType
from fonely.models.schema import PendingAction
from fonely.services.pending_actions import PendingActionService

NOW = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)


def payload(quantity: str = "2.00") -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_name": "Example Customer",
            "customer_phone": "+919123456789",
            "pickup_at": "2026-08-01T10:00:00Z",
            "lines": [{"product_id": 7, "quantity": quantity}],
            "customer_note": None,
        },
    }


def actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        session_id="session-1",
    )


def commit_context(version: int = 1) -> CommitResultContext:
    return CommitResultContext(
        business_id=1,
        pending_action_id=11,
        expected_version=version,
        engine="order_engine",
    )


def action(
    status: PendingActionStatus = PendingActionStatus.COLLECTING_DETAILS,
    version: int = 1,
    *,
    quantity: str = "2.00",
    expires_at: datetime = NOW + timedelta(minutes=15),
    snapshot: str | None = None,
) -> PendingAction:
    validated = validate_payload(PendingActionType.ORDER, 1, payload(quantity))
    return PendingAction(
        id=11,
        business_id=1,
        session_id="session-1",
        action_type=PendingActionType.ORDER.value,
        payload_schema_version=1,
        proposed_payload=canonical_payload_dict(validated),
        payload_digest=payload_digest(validated),
        confirmation_snapshot=snapshot,
        status=status.value,
        expires_at=expires_at,
        idempotency_key="key-1",
        initiated_by="+919123456789",
        confirmed_by=None,
        committed_entity_type=None,
        committed_entity_id=None,
        commit_error_code=None,
        commit_error_message=None,
        rejection_reason_code=None,
        version=version,
        created_at=NOW,
        updated_at=NOW,
        confirmed_at=None,
    )


class FakeRepo:
    def __init__(self, current: PendingAction) -> None:
        self.current = current
        self.last_update: dict[str, Any] | None = None

    async def get_by_id(self, business_id: int, action_id: int) -> PendingAction | None:
        if self.current.business_id == business_id and self.current.id == action_id:
            return self.current
        return None

    async def conditional_update(self, **kwargs: Any) -> PendingAction | None:
        self.last_update = kwargs
        if self.current.version != kwargs["expected_version"]:
            return None
        if self.current.status != kwargs["expected_status"].value:
            return None
        for key, value in kwargs["values"].items():
            setattr(self.current, key, value)
        self.current.version += 1
        return self.current


@pytest.fixture
def service_factory() -> Any:
    def make(current: PendingAction) -> tuple[PendingActionService, FakeRepo]:
        service = PendingActionService(AsyncMock())
        repo = FakeRepo(current)
        service._repo = repo  # type: ignore[assignment]
        return service, repo

    return make


@pytest.fixture(autouse=True)
def authorize() -> Any:
    with (
        patch(
            "fonely.services.pending_actions.require_action_permission",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            PendingActionService,
            "_validate_new_payload_products",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            PendingActionService,
            "_validate_stored_payload_ownership",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            PendingActionService,
            "_require_committed_entity",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield


async def test_revision_clears_confirmation_and_commit_metadata(service_factory: Any) -> None:
    current = action(
        PendingActionStatus.AWAITING_CONFIRMATION,
        snapshot="old-snapshot",
    )
    current.confirmed_by = "+919123456789"
    current.confirmed_at = NOW
    current.committed_entity_type = "order"
    current.committed_entity_id = 99
    current.commit_error_code = "old"
    current.commit_error_message = "old message"
    service, repo = service_factory(current)
    revised = await service.revise(
        RevisePendingActionCommand(
            actor=actor(),
            action_id=11,
            expected_version=1,
            payload_schema_version=1,
            payload=payload("3.00"),
        )
    )
    assert revised.status == PendingActionStatus.COLLECTING_DETAILS
    assert revised.confirmation_snapshot is None
    assert revised.committed_entity_id is None
    assert revised.error_code is None
    assert revised.version == 2
    assert repo.last_update is not None
    assert repo.last_update["values"]["confirmation_snapshot"] is None


async def test_mark_awaiting_generates_internal_snapshot(service_factory: Any) -> None:
    service, _ = service_factory(action())
    with patch("fonely.services.pending_actions.utcnow", return_value=NOW):
        result = await service.mark_awaiting_confirmation(
            MarkAwaitingConfirmationCommand(
                actor=actor(),
                action_id=11,
                expected_version=1,
            )
        )
    expected = confirmation_snapshot(validate_payload(PendingActionType.ORDER, 1, payload()))
    assert result.status == PendingActionStatus.AWAITING_CONFIRMATION
    assert result.confirmation_snapshot == expected


async def test_begin_commit_uses_expected_version_and_state(service_factory: Any) -> None:
    service, repo = service_factory(
        action(PendingActionStatus.AWAITING_CONFIRMATION, snapshot="snapshot")
    )
    with patch("fonely.services.pending_actions.utcnow", return_value=NOW):
        result = await service.begin_commit(BeginCommitCommand(context=commit_context()))
    assert result.status == PendingActionStatus.COMMITTING
    assert repo.last_update is not None
    assert repo.last_update["expected_version"] == 1
    assert repo.last_update["expected_status"] == PendingActionStatus.AWAITING_CONFIRMATION


async def test_stale_version_raises_concurrency_error(service_factory: Any) -> None:
    current = action(PendingActionStatus.AWAITING_CONFIRMATION, version=2, snapshot="snapshot")
    service, _ = service_factory(current)
    with (
        patch("fonely.services.pending_actions.utcnow", return_value=NOW),
        pytest.raises(PendingActionConcurrencyError),
    ):
        await service.begin_commit(BeginCommitCommand(context=commit_context()))


async def test_complete_commit_idempotent_same_entity(service_factory: Any) -> None:
    current = action(PendingActionStatus.CONFIRMED, version=4, snapshot="snapshot")
    current.committed_entity_type = "order"
    current.committed_entity_id = 88
    service, _ = service_factory(current)
    result = await service.complete_commit(
        CompleteCommitCommand(
            context=commit_context(version=4),
            committed_entity_type="order",
            committed_entity_id=88,
        )
    )
    assert result.status == PendingActionStatus.CONFIRMED
    assert result.committed_entity_id == 88


async def test_complete_commit_conflicts_different_entity(service_factory: Any) -> None:
    current = action(PendingActionStatus.CONFIRMED, version=4, snapshot="snapshot")
    current.committed_entity_type = "order"
    current.committed_entity_id = 88
    service, _ = service_factory(current)
    with pytest.raises(CommitEntityConflictError):
        await service.complete_commit(
            CompleteCommitCommand(
                context=commit_context(version=4),
                committed_entity_type="order",
                committed_entity_id=89,
            )
        )


@pytest.mark.parametrize(
    ("retryable", "expected_status"),
    [
        (True, PendingActionStatus.AWAITING_CONFIRMATION),
        (False, PendingActionStatus.REJECTED),
    ],
)
async def test_fail_commit_policy(
    service_factory: Any,
    retryable: bool,
    expected_status: PendingActionStatus,
) -> None:
    service, _ = service_factory(action(PendingActionStatus.COMMITTING, snapshot="snapshot"))
    result = await service.fail_commit(
        FailCommitCommand(
            context=commit_context(),
            error_code="temporary_conflict" if retryable else "invalid_product",
            retryable=retryable,
        )
    )
    assert result.status == expected_status
    assert result.error_code == ("temporary_conflict" if retryable else "invalid_product")


async def test_cancel_idempotent_only_if_already_cancelled(service_factory: Any) -> None:
    service, _ = service_factory(action(PendingActionStatus.CANCELLED, version=2))
    result = await service.cancel(
        CancelPendingActionCommand(actor=actor(), action_id=11, expected_version=1)
    )
    assert result.status == PendingActionStatus.CANCELLED


async def test_cancel_confirmed_rejected(service_factory: Any) -> None:
    service, _ = service_factory(action(PendingActionStatus.CONFIRMED))
    with pytest.raises(InvalidStateTransitionError):
        await service.cancel(
            CancelPendingActionCommand(actor=actor(), action_id=11, expected_version=1)
        )


async def test_expire_exact_boundary(service_factory: Any) -> None:
    service, _ = service_factory(action(expires_at=NOW))
    result = await service.expire(
        ExpirePendingActionCommand(
            business_id=1,
            action_id=11,
            expected_version=1,
            now=NOW,
        )
    )
    assert result.status == PendingActionStatus.EXPIRED


def test_expiry_command_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        ExpirePendingActionCommand(
            business_id=1,
            action_id=11,
            expected_version=1,
            now=datetime(2026, 8, 1, 8, 0),
        )


def test_public_result_is_immutable_and_contains_no_orm_state(service_factory: Any) -> None:
    service, _ = service_factory(action())
    result = service._to_result(action())
    assert "_sa_instance_state" not in result.model_dump()
    with pytest.raises(ValidationError):
        result.id = 99
