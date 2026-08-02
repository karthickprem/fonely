"""Pure order-domain contract, pricing, and lifecycle tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fonely.domain.orders.calculations import AuthoritativeProduct, price_order_lines
from fonely.domain.orders.commands import ConfirmOrderLine, ConfirmPendingOrderCommand
from fonely.domain.orders.errors import OrderReservationExpiredError, OrderStateTransitionError
from fonely.domain.orders.policies import (
    require_cancellable,
    require_pickup,
    reservation_is_expired,
)
from fonely.domain.pending_actions.commands import ActorContext, CommitResultContext
from fonely.models.enums import CallerRole, OrderStatus, ProductUnit


def actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
    )


def context() -> CommitResultContext:
    return CommitResultContext(
        business_id=1,
        pending_action_id=10,
        expected_version=3,
        engine="order_engine",
    )


def test_order_lines_are_sorted_and_duplicates_rejected() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    command = ConfirmPendingOrderCommand(
        context=context(),
        actor=actor(),
        lines=(
            ConfirmOrderLine(product_id=2, quantity="1"),
            ConfirmOrderLine(product_id=1, quantity="2"),
        ),
        now=now,
        reservation_expires_at=now + timedelta(hours=1),
        idempotency_key="order-1",
    )
    assert [line.product_id for line in command.lines] == [1, 2]
    with pytest.raises(ValidationError):
        ConfirmPendingOrderCommand(
            context=context(),
            actor=actor(),
            lines=(
                ConfirmOrderLine(product_id=1, quantity="1"),
                ConfirmOrderLine(product_id=1, quantity="2"),
            ),
            now=now,
            reservation_expires_at=now + timedelta(hours=1),
            idempotency_key="order-2",
        )


def test_price_snapshots_use_authoritative_products_and_half_up_quantization() -> None:
    products = {
        1: AuthoritativeProduct(1, "Rice", ProductUnit.KG, Decimal("12.35")),
        2: AuthoritativeProduct(2, "Oil", ProductUnit.LITRE, Decimal("10.00")),
    }
    pricing = price_order_lines(
        {2: Decimal("1"), 1: Decimal("1.25")},
        products,
    )
    assert [line.product_id for line in pricing.lines] == [1, 2]
    assert pricing.lines[0].subtotal == Decimal("15.44")
    assert pricing.total == Decimal("25.44")


def test_terminal_transitions_and_exact_expiry_boundary() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    assert require_cancellable(OrderStatus.CANCELLED) is False
    with pytest.raises(OrderStateTransitionError):
        require_cancellable(OrderStatus.PICKED_UP)
    assert require_pickup(OrderStatus.PICKED_UP, now + timedelta(hours=1), now) is False
    with pytest.raises(OrderStateTransitionError, match="cancelled order"):
        require_pickup(OrderStatus.CANCELLED, now + timedelta(hours=1), now)
    with pytest.raises(OrderReservationExpiredError):
        require_pickup(OrderStatus.CONFIRMED, now, now)
    assert reservation_is_expired(now, now)
