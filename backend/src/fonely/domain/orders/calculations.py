"""Pure authoritative order snapshot and total calculations."""

from dataclasses import dataclass
from decimal import Decimal

from fonely.core.validators import quantize_inr
from fonely.domain.inventory.policies import MAX_QUANTITY
from fonely.domain.orders.errors import OrderTotalOverflowError
from fonely.models.enums import ProductUnit

MAX_MONEY = Decimal("99999999.99")


@dataclass(frozen=True, slots=True)
class AuthoritativeProduct:
    id: int
    name: str
    unit: ProductUnit
    price_per_unit: Decimal


@dataclass(frozen=True, slots=True)
class OrderLineSnapshot:
    product_id: int
    product_name: str
    unit: ProductUnit
    quantity: Decimal
    price_per_unit: Decimal
    subtotal: Decimal


@dataclass(frozen=True, slots=True)
class OrderPricing:
    lines: tuple[OrderLineSnapshot, ...]
    total: Decimal


def price_order_lines(
    quantities: dict[int, Decimal],
    products: dict[int, AuthoritativeProduct],
) -> OrderPricing:
    snapshots: list[OrderLineSnapshot] = []
    total = Decimal(0)
    for product_id in sorted(quantities):
        quantity = quantities[product_id]
        if quantity <= 0 or quantity > MAX_QUANTITY:
            raise ValueError("Order quantity is outside supported precision")
        product = products[product_id]
        subtotal = quantize_inr(quantity * product.price_per_unit)
        if subtotal > MAX_MONEY:
            raise OrderTotalOverflowError("Order line subtotal exceeds database precision")
        total += subtotal
        if total > MAX_MONEY:
            raise OrderTotalOverflowError("Order total exceeds database precision")
        snapshots.append(
            OrderLineSnapshot(
                product_id=product.id,
                product_name=product.name,
                unit=product.unit,
                quantity=quantity,
                price_per_unit=product.price_per_unit,
                subtotal=subtotal,
            )
        )
    return OrderPricing(lines=tuple(snapshots), total=quantize_inr(total))
