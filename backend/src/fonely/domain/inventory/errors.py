"""Typed inventory-domain errors with stable external mapping codes."""

from fonely.core.exceptions import FonelyError


class InventoryError(FonelyError):
    code = "inventory_error"


class InventoryBalanceNotFoundError(InventoryError):
    code = "inventory_balance_not_found"


class InvalidProductError(InventoryError):
    code = "invalid_product"


class InsufficientAvailableStockError(InventoryError):
    code = "insufficient_available_stock"


class InsufficientOnHandStockError(InventoryError):
    code = "insufficient_on_hand_stock"


class ReservedStockViolationError(InventoryError):
    code = "reserved_stock_prevents_reduction"


class InvalidQuantityError(InventoryError):
    code = "invalid_quantity"


class InventoryStateTransitionError(InventoryError):
    code = "inventory_state_transition_invalid"


class InventoryIdempotencyConflictError(InventoryError):
    code = "inventory_idempotency_conflict"


class InventoryTenantMismatchError(InventoryError):
    code = "inventory_tenant_mismatch"


class InventoryStaleVersionError(InventoryError):
    code = "inventory_stale_version"


class LedgerInconsistencyError(InventoryError):
    code = "inventory_ledger_inconsistent"
