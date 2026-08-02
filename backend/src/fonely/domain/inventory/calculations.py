"""Pure inventory transition calculations."""

from dataclasses import dataclass
from decimal import Decimal

from fonely.domain.inventory.errors import (
    InsufficientAvailableStockError,
    InsufficientOnHandStockError,
    InventoryStateTransitionError,
    ReservedStockViolationError,
)
from fonely.models.enums import InventoryMovementType


@dataclass(frozen=True, slots=True)
class InventoryState:
    on_hand: Decimal
    reserved: Decimal

    @property
    def available(self) -> Decimal:
        return self.on_hand - self.reserved


@dataclass(frozen=True, slots=True)
class InventoryTransition:
    before: InventoryState
    requested_quantity: Decimal
    on_hand_delta: Decimal
    reserved_delta: Decimal
    after: InventoryState
    movement_type: InventoryMovementType


def _transition(
    before: InventoryState,
    quantity: Decimal,
    on_hand_delta: Decimal,
    reserved_delta: Decimal,
    movement_type: InventoryMovementType,
) -> InventoryTransition:
    after = InventoryState(
        on_hand=before.on_hand + on_hand_delta,
        reserved=before.reserved + reserved_delta,
    )
    if after.on_hand < 0:
        raise InsufficientOnHandStockError("Operation would make on-hand stock negative")
    if after.reserved < 0:
        raise InventoryStateTransitionError("Operation would make reserved stock negative")
    if after.reserved > after.on_hand:
        raise ReservedStockViolationError("Operation would consume reserved stock")
    return InventoryTransition(
        before=before,
        requested_quantity=quantity,
        on_hand_delta=on_hand_delta,
        reserved_delta=reserved_delta,
        after=after,
        movement_type=movement_type,
    )


def add_stock(before: InventoryState, quantity: Decimal) -> InventoryTransition:
    return _transition(
        before,
        quantity,
        quantity,
        Decimal(0),
        InventoryMovementType.STOCK_ADDED,
    )


def set_stock(before: InventoryState, target: Decimal) -> InventoryTransition:
    if target < before.reserved:
        raise ReservedStockViolationError("Target stock is below reserved stock")
    return _transition(
        before,
        target,
        target - before.on_hand,
        Decimal(0),
        InventoryMovementType.MANUAL_ADJUSTMENT,
    )


def sell_walk_in(before: InventoryState, quantity: Decimal) -> InventoryTransition:
    if quantity > before.available:
        raise InsufficientAvailableStockError("Walk-in sale would consume reserved stock")
    return _transition(
        before,
        quantity,
        -quantity,
        Decimal(0),
        InventoryMovementType.WALK_IN_SALE,
    )


def reserve_stock(before: InventoryState, quantity: Decimal) -> InventoryTransition:
    if quantity > before.available:
        raise InsufficientAvailableStockError("Insufficient available stock")
    return _transition(
        before,
        quantity,
        Decimal(0),
        quantity,
        InventoryMovementType.PHONE_ORDER_RESERVED,
    )


def release_reservation(
    before: InventoryState,
    quantity: Decimal,
    movement_type: InventoryMovementType,
) -> InventoryTransition:
    if movement_type not in {
        InventoryMovementType.ORDER_CANCELLED,
        InventoryMovementType.RESERVATION_RELEASED,
    }:
        raise InventoryStateTransitionError("Invalid reservation-release movement type")
    if quantity > before.reserved:
        raise InventoryStateTransitionError("Release exceeds reserved stock")
    return _transition(before, quantity, Decimal(0), -quantity, movement_type)


def complete_pickup(before: InventoryState, quantity: Decimal) -> InventoryTransition:
    if quantity > before.reserved:
        raise InventoryStateTransitionError("Pickup exceeds reserved stock")
    return _transition(
        before,
        quantity,
        -quantity,
        -quantity,
        InventoryMovementType.ORDER_COMPLETED,
    )
