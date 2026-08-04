"""Cost-per-success calculations with explicit evidence gaps."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CostComponent:
    name: str
    amount: Decimal | None
    currency: str
    source: str | None
    effective_date: str | None
    status: str = "estimated"


@dataclass(frozen=True)
class BookingEconomics:
    total_cost: Decimal | None
    cost_per_verified_booking: Decimal | None
    currency: str
    successful_bookings: int
    evidence_gaps: tuple[str, ...]


def calculate_cost_per_verified_booking(
    components: list[CostComponent],
    successful_bookings: int,
) -> BookingEconomics:
    currencies = {component.currency for component in components}
    if len(currencies) != 1:
        raise ValueError("all cost components must use one currency")
    currency = next(iter(currencies), "INR")
    gaps = tuple(
        f"{component.name}: missing amount or source"
        for component in components
        if component.amount is None or not component.source
    )
    if gaps:
        return BookingEconomics(None, None, currency, successful_bookings, gaps)
    total = sum((component.amount for component in components if component.amount is not None), Decimal("0"))
    per_success = total / successful_bookings if successful_bookings > 0 else None
    if successful_bookings <= 0:
        gaps = (*gaps, "no independently verified successful bookings")
    return BookingEconomics(total, per_success, currency, successful_bookings, gaps)
