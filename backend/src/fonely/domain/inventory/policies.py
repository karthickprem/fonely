"""Inventory value and authorization-boundary policies."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BeforeValidator

MAX_QUANTITY = Decimal("99999999.99")
_QUANTUM = Decimal("0.01")


def _coerce_quantity(value: object) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (bool, float)):
        raise ValueError("Quantity must be supplied as Decimal, integer, or string")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Quantity is not a valid decimal") from exc
    else:
        raise ValueError("Quantity must be supplied as Decimal, integer, or string")
    if not result.is_finite():
        raise ValueError("Quantity must be finite")
    return result


def _validate_scale_and_range(value: Decimal) -> Decimal:
    if value != value.quantize(_QUANTUM):
        raise ValueError("Quantity must have at most two decimal places")
    if abs(value) > MAX_QUANTITY:
        raise ValueError("Quantity exceeds database precision")
    return value


def _nonnegative(value: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("Quantity must be non-negative")
    return value


def _positive(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("Quantity must be positive")
    return value


InventoryQuantity = Annotated[
    Decimal,
    BeforeValidator(_coerce_quantity),
    AfterValidator(_validate_scale_and_range),
    AfterValidator(_nonnegative),
]
PositiveInventoryQuantity = Annotated[
    Decimal,
    BeforeValidator(_coerce_quantity),
    AfterValidator(_validate_scale_and_range),
    AfterValidator(_positive),
]
SignedInventoryQuantity = Annotated[
    Decimal,
    BeforeValidator(_coerce_quantity),
    AfterValidator(_validate_scale_and_range),
]


def derive_business_date(instant: datetime, timezone: str) -> date:
    """Derive the tenant-local business date from an authoritative aware instant."""
    if instant.tzinfo is None:
        raise ValueError("Authoritative timestamp must be timezone-aware")
    return instant.astimezone(ZoneInfo(timezone)).date()


@dataclass(frozen=True, slots=True)
class DirectInventoryRequestSignature:
    """Pure Stage A identity; durable uniqueness requires migration 0005."""

    business_id: int
    operation: Literal["set", "add", "walk_in"]
    product_id: int
    quantity: Decimal
    occurred_at: datetime
    note: str | None

    @property
    def digest(self) -> str:
        if self.occurred_at.tzinfo is None:
            raise ValueError("Operation timestamp must be timezone-aware")
        payload = {
            "business_id": self.business_id,
            "note": self.note,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "operation": self.operation,
            "product_id": self.product_id,
            "quantity": format(self.quantity.normalize(), "f"),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def direct_inventory_requests_equivalent(
    first: DirectInventoryRequestSignature,
    second: DirectInventoryRequestSignature,
) -> bool:
    return first == second and first.digest == second.digest
