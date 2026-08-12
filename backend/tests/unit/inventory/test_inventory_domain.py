"""Pure inventory-domain contract and transition tests."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fonely.domain.inventory.calculations import (
    InventoryState,
    add_stock,
    complete_pickup,
    release_reservation,
    reserve_stock,
    sell_walk_in,
    set_stock,
)
from fonely.domain.inventory.commands import AddOwnerStockCommand, SetOwnerStockCommand
from fonely.domain.inventory.errors import (
    InsufficientAvailableStockError,
    ReservedStockViolationError,
)
from fonely.domain.inventory.policies import derive_business_date
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole, Channel, InventoryMovementType


def actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.OWNER,
        channel=Channel.TEXT,
    )


@pytest.mark.parametrize("value", [True, 1.2, "NaN", "Infinity", "1.001", "100000000"])
def test_inventory_quantity_rejects_invalid_boundary_values(value: object) -> None:
    with pytest.raises(ValidationError):
        AddOwnerStockCommand(
            actor=actor(),
            product_id=1,
            quantity=value,
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
            idempotency_key="add-1",
        )


def test_set_allows_zero_but_add_requires_positive() -> None:
    command = SetOwnerStockCommand(
        actor=actor(),
        product_id=1,
        quantity="0",
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        idempotency_key="set-1",
    )
    assert command.quantity == 0
    with pytest.raises(ValidationError):
        AddOwnerStockCommand(
            actor=actor(),
            product_id=1,
            quantity="0",
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
            idempotency_key="add-1",
        )


def test_business_date_uses_tenant_timezone() -> None:
    instant = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)
    assert derive_business_date(instant, "Asia/Kolkata").isoformat() == "2026-08-02"


def test_split_delta_transition_sequence_preserves_invariants() -> None:
    state = InventoryState(Decimal("0"), Decimal("0"))
    added = add_stock(state, Decimal("10"))
    reserved = reserve_stock(added.after, Decimal("3"))
    picked_up = complete_pickup(reserved.after, Decimal("2"))
    released = release_reservation(
        picked_up.after,
        Decimal("1"),
        InventoryMovementType.ORDER_CANCELLED,
    )
    assert added.on_hand_delta == Decimal("10")
    assert reserved.reserved_delta == Decimal("3")
    assert picked_up.on_hand_delta == picked_up.reserved_delta == Decimal("-2")
    assert released.reserved_delta == Decimal("-1")
    assert released.after == InventoryState(Decimal("8"), Decimal("0"))


def test_set_and_walk_in_cannot_consume_reserved_stock() -> None:
    state = InventoryState(Decimal("5"), Decimal("3"))
    with pytest.raises(ReservedStockViolationError):
        set_stock(state, Decimal("2"))
    with pytest.raises(InsufficientAvailableStockError):
        sell_walk_in(state, Decimal("3"))
