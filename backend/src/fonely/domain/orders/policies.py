"""Pure order lifecycle policies."""

from datetime import datetime

from fonely.domain.orders.errors import OrderReservationExpiredError, OrderStateTransitionError
from fonely.models.enums import OrderStatus


def require_cancellable(status: OrderStatus) -> bool:
    """Return False for an idempotent cancellation replay, otherwise validate."""
    if status is OrderStatus.CANCELLED:
        return False
    if status is OrderStatus.PICKED_UP:
        raise OrderStateTransitionError("A picked-up order cannot be cancelled")
    return True


def require_pickup(status: OrderStatus, expires_at: datetime, now: datetime) -> bool:
    """Return False for an idempotent pickup replay, otherwise validate."""
    if status is OrderStatus.PICKED_UP:
        return False
    if status is OrderStatus.CANCELLED:
        raise OrderStateTransitionError("A cancelled order cannot be picked up")
    if expires_at <= now:
        raise OrderReservationExpiredError("Order reservation has expired")
    return True


def reservation_is_expired(expires_at: datetime, now: datetime) -> bool:
    if expires_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("Expiry comparison requires aware datetimes")
    return expires_at <= now
