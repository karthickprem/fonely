"""Strict order operation commands."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fonely.core.validators import AwareDatetime
from fonely.domain.inventory.policies import PositiveInventoryQuantity
from fonely.domain.pending_actions.commands import ActorContext, CommitResultContext


class OrderCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ConfirmOrderLine(OrderCommand):
    product_id: Annotated[int, Field(gt=0)]
    quantity: PositiveInventoryQuantity


class ConfirmPendingOrderCommand(OrderCommand):
    context: CommitResultContext
    actor: ActorContext
    lines: Annotated[tuple[ConfirmOrderLine, ...], Field(min_length=1, max_length=50)]
    now: AwareDatetime
    reservation_expires_at: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def canonicalize_lines(self) -> "ConfirmPendingOrderCommand":
        if self.actor.business_id != self.context.business_id:
            raise ValueError("Actor and commit context must use the same business")
        ids = [line.product_id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate product lines are not allowed")
        ordered = tuple(sorted(self.lines, key=lambda line: line.product_id))
        if ordered != self.lines:
            object.__setattr__(self, "lines", ordered)
        if self.reservation_expires_at <= self.now:
            raise ValueError("Reservation expiry must be after confirmation time")
        return self


class GetOrderQuery(OrderCommand):
    actor: ActorContext
    order_id: Annotated[int, Field(gt=0)]


class CancelOrderCommand(OrderCommand):
    actor: ActorContext
    order_id: Annotated[int, Field(gt=0)]
    now: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]


class ExpireOrderReservationsCommand(OrderCommand):
    now: AwareDatetime
    batch_size: Annotated[int, Field(gt=0, le=1000)] = 100


class CompletePickupCommand(OrderCommand):
    actor: ActorContext
    order_id: Annotated[int, Field(gt=0)]
    now: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]
