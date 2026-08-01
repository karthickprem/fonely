"""Deterministic actor authorization backed by BusinessUser membership."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.errors import PendingActionUnauthorizedError
from fonely.models.enums import BusinessUserRole, CallerRole, PendingActionType
from fonely.models.schema import BusinessUser, PendingAction

_OWNER_ACTIONS = frozenset(
    {
        PendingActionType.OWNER_STOCK_UPDATE,
        PendingActionType.OWNER_PRICE_UPDATE,
        PendingActionType.OWNER_SCHEDULE_UPDATE,
    }
)
_CUSTOMER_ACTIONS = frozenset({PendingActionType.ORDER, PendingActionType.APPOINTMENT})


def assert_verified_role_permits(
    actor: ActorContext,
    action_type: PendingActionType,
) -> None:
    """Pure role policy; database membership is checked separately."""
    if actor.business_id <= 0:
        raise PendingActionUnauthorizedError("A valid business scope is required")
    if action_type in _OWNER_ACTIONS and actor.verified_role not in {
        CallerRole.OWNER,
        CallerRole.MANAGER,
    }:
        raise PendingActionUnauthorizedError("Verified owner or manager role required")
    if action_type not in _OWNER_ACTIONS | _CUSTOMER_ACTIONS:
        raise PendingActionUnauthorizedError(f"Action type not permitted: {action_type.value}")


async def require_active_business_user(
    session: AsyncSession,
    actor: ActorContext,
) -> BusinessUser:
    statement = select(BusinessUser).where(
        BusinessUser.business_id == actor.business_id,
        BusinessUser.phone == actor.normalized_phone,
        BusinessUser.is_active.is_(True),
    )
    user = (await session.execute(statement)).scalar_one_or_none()
    if user is None:
        raise PendingActionUnauthorizedError("Active business user membership required")
    return user


async def require_owner_or_manager(
    session: AsyncSession,
    actor: ActorContext,
) -> BusinessUser:
    user = await require_active_business_user(session, actor)
    if user.role not in {BusinessUserRole.OWNER.value, BusinessUserRole.MANAGER.value}:
        raise PendingActionUnauthorizedError("Owner or manager permission required")
    return user


async def require_existing_action_permission(
    session: AsyncSession,
    actor: ActorContext,
    action: PendingAction,
) -> None:
    """Authorize mutation of an existing tenant-scoped action."""
    if action.business_id != actor.business_id:
        raise PendingActionUnauthorizedError("Cross-business action access is forbidden")
    action_type = PendingActionType(action.action_type)
    assert_verified_role_permits(actor, action_type)
    if actor.verified_role in {CallerRole.OWNER, CallerRole.MANAGER}:
        await require_owner_or_manager(session, actor)
        return
    if action_type not in _CUSTOMER_ACTIONS:
        raise PendingActionUnauthorizedError("Customer cannot mutate owner action")
    if action.initiated_by != actor.normalized_phone:
        raise PendingActionUnauthorizedError("Customer may mutate only their own action")


async def require_action_permission(
    session: AsyncSession,
    actor: ActorContext,
    action_type: PendingActionType,
) -> None:
    assert_verified_role_permits(actor, action_type)
    if action_type in _OWNER_ACTIONS:
        await require_owner_or_manager(session, actor)
        return
    if action_type in _CUSTOMER_ACTIONS:
        if actor.verified_role in {CallerRole.OWNER, CallerRole.MANAGER}:
            await require_active_business_user(session, actor)
        return
    raise PendingActionUnauthorizedError(f"Action type not permitted: {action_type.value}")
