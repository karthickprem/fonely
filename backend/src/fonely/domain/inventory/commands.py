"""Strict inventory operation commands."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fonely.core.validators import AwareDatetime
from fonely.domain.inventory.policies import InventoryQuantity, PositiveInventoryQuantity
from fonely.domain.pending_actions.commands import ActorContext, CommitResultContext


class InventoryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GetInventoryAvailabilityQuery(InventoryCommand):
    business_id: Annotated[int, Field(gt=0)]
    product_ids: tuple[Annotated[int, Field(gt=0)], ...] = ()
    at: AwareDatetime


class CommitOwnerStockCommand(InventoryCommand):
    context: CommitResultContext
    actor: ActorContext
    operation: Literal["set", "add"]
    quantity: InventoryQuantity
    occurred_at: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def validate_business_scope(self) -> "CommitOwnerStockCommand":
        if self.actor.business_id != self.context.business_id:
            raise ValueError("Actor and commit context must use the same business")
        return self


class SetOwnerStockCommand(InventoryCommand):
    actor: ActorContext
    product_id: Annotated[int, Field(gt=0)]
    quantity: InventoryQuantity
    occurred_at: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]
    note: Annotated[str | None, Field(default=None, max_length=500)]


class AddOwnerStockCommand(InventoryCommand):
    actor: ActorContext
    product_id: Annotated[int, Field(gt=0)]
    quantity: PositiveInventoryQuantity
    occurred_at: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]
    note: Annotated[str | None, Field(default=None, max_length=500)]


class RecordWalkInSaleCommand(InventoryCommand):
    actor: ActorContext
    product_id: Annotated[int, Field(gt=0)]
    quantity: PositiveInventoryQuantity
    occurred_at: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]
    note: Annotated[str | None, Field(default=None, max_length=500)]


class ReleaseExpiredReservationsCommand(InventoryCommand):
    now: AwareDatetime
    batch_size: Annotated[int, Field(gt=0, le=1000)] = 100


class VerifyLedgerConsistencyQuery(InventoryCommand):
    business_id: Annotated[int, Field(gt=0)]
    product_id: Annotated[int | None, Field(default=None, gt=0)]
