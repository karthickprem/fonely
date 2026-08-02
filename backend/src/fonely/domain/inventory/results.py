"""Immutable inventory operation results."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from fonely.models.enums import InventoryMovementType, ProductUnit


class InventoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InventoryAvailabilityResult(InventoryResult):
    business_id: int
    product_id: int
    product_name: str
    unit: ProductUnit
    business_date: date
    on_hand_qty: Decimal
    reserved_qty: Decimal
    available_qty: Decimal


class InventoryMutationResult(InventoryResult):
    business_id: int
    product_id: int
    business_date: date
    movement_id: int
    movement_type: InventoryMovementType
    on_hand_delta: Decimal
    reserved_delta: Decimal
    on_hand_after: Decimal
    reserved_after: Decimal
    available_after: Decimal
    idempotent_replay: bool = False


class LedgerDiscrepancy(InventoryResult):
    business_id: int
    product_id: int
    business_date: date
    balance_on_hand: Decimal
    ledger_on_hand: Decimal
    balance_reserved: Decimal
    ledger_reserved: Decimal


class LedgerConsistencyResult(InventoryResult):
    business_id: int
    discrepancies: tuple[LedgerDiscrepancy, ...]

    @property
    def consistent(self) -> bool:
        return not self.discrepancies


class ReservationExpiryResult(InventoryResult):
    expired_reservation_ids: tuple[int, ...]
    count: int
