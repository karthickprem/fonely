"""Immutable order operation results."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from fonely.models.enums import OrderStatus, ProductUnit


class OrderResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OrderLineResult(OrderResultModel):
    id: int
    product_id: int
    product_name: str
    quantity: Decimal
    unit: ProductUnit
    price_per_unit: Decimal
    subtotal: Decimal


class OrderResult(OrderResultModel):
    id: int
    business_id: int
    status: OrderStatus
    customer_name: str | None
    customer_phone: str
    total_amount: Decimal
    pickup_at: datetime | None
    reservation_expires_at: datetime | None
    lines: tuple[OrderLineResult, ...]
    idempotent_replay: bool = False


class OrderExpiryResult(OrderResultModel):
    expired_order_ids: tuple[int, ...]
    expired_reservation_ids: tuple[int, ...]
    count: int
