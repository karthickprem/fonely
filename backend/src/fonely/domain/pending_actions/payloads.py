"""Strict, versioned PendingAction payload envelopes."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fonely.core.validators import AwareDatetime, E164PhoneNumber, ISODate, Quantity
from fonely.domain.pending_actions.errors import UnsupportedPayloadSchemaError
from fonely.models.enums import PendingActionType

PAYLOAD_SCHEMA_VERSION = 1


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PendingOrderLine(StrictModel):
    product_id: Annotated[int, Field(gt=0)]
    quantity: Quantity


class PendingOrderData(StrictModel):
    customer_name: Annotated[str | None, Field(default=None, max_length=200)]
    customer_phone: E164PhoneNumber
    pickup_at: AwareDatetime
    lines: Annotated[list[PendingOrderLine], Field(min_length=1, max_length=50)]
    customer_note: Annotated[str | None, Field(default=None, max_length=500)]

    @model_validator(mode="after")
    def canonicalize_product_lines(self) -> "PendingOrderData":
        product_ids = [line.product_id for line in self.lines]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("Duplicate product lines are not allowed")
        self.lines.sort(key=lambda line: line.product_id)
        return self


class OwnerStockUpdateData(StrictModel):
    product_id: Annotated[int, Field(gt=0)]
    business_date: ISODate
    operation: Literal["set", "add"]
    quantity: Quantity
    note: Annotated[str | None, Field(default=None, max_length=500)]


class PendingOrderEnvelope(StrictModel):
    schema_version: Literal[1] = 1
    action_type: Literal[PendingActionType.ORDER] = PendingActionType.ORDER
    data: PendingOrderData


class OwnerStockUpdateEnvelope(StrictModel):
    schema_version: Literal[1] = 1
    action_type: Literal[PendingActionType.OWNER_STOCK_UPDATE] = (
        PendingActionType.OWNER_STOCK_UPDATE
    )
    data: OwnerStockUpdateData


type PayloadEnvelope = PendingOrderEnvelope | OwnerStockUpdateEnvelope
type PayloadEnvelopeAdapter = Annotated[PayloadEnvelope, Field(discriminator="action_type")]

_PAYLOAD_REGISTRY: dict[
    tuple[PendingActionType, int], type[PendingOrderEnvelope] | type[OwnerStockUpdateEnvelope]
] = {
    (PendingActionType.ORDER, PAYLOAD_SCHEMA_VERSION): PendingOrderEnvelope,
    (
        PendingActionType.OWNER_STOCK_UPDATE,
        PAYLOAD_SCHEMA_VERSION,
    ): OwnerStockUpdateEnvelope,
}


def validate_payload(
    action_type: PendingActionType,
    schema_version: int,
    payload: object,
) -> PayloadEnvelope:
    model = _PAYLOAD_REGISTRY.get((action_type, schema_version))
    if model is None:
        raise UnsupportedPayloadSchemaError(
            f"Unsupported payload schema: action={action_type.value}, version={schema_version}"
        )
    validated = model.model_validate(payload)
    if validated.action_type != action_type:
        raise ValueError("Payload action_type does not match pending action")
    if validated.schema_version != schema_version:
        raise ValueError("Payload schema_version does not match pending action")
    return validated
