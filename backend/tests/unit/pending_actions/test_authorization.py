"""Pure actor-role authorization tests."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.errors import PendingActionUnauthorizedError
from fonely.models.enums import (
    BusinessUserRole,
    CallerRole,
    Channel,
    PendingActionStatus,
    PendingActionType,
)
from fonely.models.schema import BusinessUser, PendingAction
from fonely.services.authorization import (
    assert_verified_role_permits,
    require_existing_action_permission,
    require_owner_or_manager,
)


def actor(role: CallerRole, business_id: int = 1) -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone="+919123456789",
        verified_role=role,
        channel=Channel.TEXT,
        session_id="session-1",
    )


@pytest.mark.parametrize("role", [CallerRole.OWNER, CallerRole.MANAGER])
def test_owner_or_manager_may_propose_stock_update(role: CallerRole) -> None:
    assert_verified_role_permits(actor(role), PendingActionType.OWNER_STOCK_UPDATE)


def test_customer_cannot_propose_stock_update() -> None:
    with pytest.raises(PendingActionUnauthorizedError):
        assert_verified_role_permits(
            actor(CallerRole.CUSTOMER),
            PendingActionType.OWNER_STOCK_UPDATE,
        )


@pytest.mark.parametrize(
    "action_type",
    [PendingActionType.ORDER, PendingActionType.APPOINTMENT],
)
def test_customer_may_propose_customer_action(action_type: PendingActionType) -> None:
    assert_verified_role_permits(actor(CallerRole.CUSTOMER), action_type)


def test_actor_context_rejects_invalid_business_scope() -> None:
    with pytest.raises(ValidationError):
        actor(CallerRole.OWNER, business_id=0)


def pending_action(
    *,
    business_id: int = 1,
    initiated_by: str = "+919123456789",
    action_type: PendingActionType = PendingActionType.ORDER,
) -> PendingAction:
    return PendingAction(
        id=1,
        business_id=business_id,
        action_type=action_type.value,
        payload_schema_version=1,
        proposed_payload={},
        payload_digest="0" * 64,
        status=PendingActionStatus.COLLECTING_DETAILS.value,
        expires_at=datetime.now(UTC),
        idempotency_key="key",
        initiated_by=initiated_by,
        version=1,
    )


async def test_customer_may_mutate_own_order_action() -> None:
    await require_existing_action_permission(
        AsyncMock(),
        actor(CallerRole.CUSTOMER),
        pending_action(),
    )


async def test_customer_cannot_mutate_other_customer_action() -> None:
    with pytest.raises(PendingActionUnauthorizedError, match="only their own"):
        await require_existing_action_permission(
            AsyncMock(),
            actor(CallerRole.CUSTOMER),
            pending_action(initiated_by="+919876543210"),
        )


async def test_cross_business_action_access_rejected() -> None:
    with pytest.raises(PendingActionUnauthorizedError, match="Cross-business"):
        await require_existing_action_permission(
            AsyncMock(),
            actor(CallerRole.CUSTOMER, business_id=2),
            pending_action(business_id=1),
        )


async def test_customer_cannot_mutate_owner_action() -> None:
    with pytest.raises(PendingActionUnauthorizedError):
        await require_existing_action_permission(
            AsyncMock(),
            actor(CallerRole.CUSTOMER),
            pending_action(action_type=PendingActionType.OWNER_STOCK_UPDATE),
        )


def session_returning(user: BusinessUser | None) -> AsyncMock:
    scalar_result = AsyncMock()
    scalar_result.scalar_one_or_none = lambda: user
    session = AsyncMock()
    session.execute.return_value = scalar_result
    return session


async def test_active_owner_membership_authorizes_owner() -> None:
    user = BusinessUser(
        id=1,
        business_id=1,
        phone="+919123456789",
        role=BusinessUserRole.OWNER.value,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    result = await require_owner_or_manager(
        session_returning(user),
        actor(CallerRole.OWNER),
    )
    assert result is user


async def test_inactive_or_missing_membership_rejected() -> None:
    with pytest.raises(PendingActionUnauthorizedError, match="membership required"):
        await require_owner_or_manager(
            session_returning(None),
            actor(CallerRole.OWNER),
        )
