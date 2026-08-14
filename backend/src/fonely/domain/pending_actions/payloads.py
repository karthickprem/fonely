"""Strict, versioned PendingAction payload envelopes."""

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fonely.core.validators import (
    AwareDatetime,
    E164PhoneNumber,
    IANATimezone,
    INRAmount,
    ISODate,
    PositiveIntegerId,
    PositiveIntegerVersion,
    Quantity,
)
from fonely.domain.appointments.datetimes import add_elapsed, instant, validate_business_local
from fonely.domain.pending_actions.errors import UnsupportedPayloadSchemaError
from fonely.models.enums import PendingActionType

PAYLOAD_SCHEMA_VERSION = 1


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FrozenStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


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


class AppointmentFacts(FrozenStrictModel):
    service_id: PositiveIntegerId
    service_name: Annotated[str, Field(min_length=1, max_length=200)]
    resource_id: PositiveIntegerId
    resource_name: Annotated[str, Field(min_length=1, max_length=200)]
    start_at: AwareDatetime
    end_at: AwareDatetime
    effective_start_at: AwareDatetime
    effective_end_at: AwareDatetime
    duration_minutes: Annotated[int, Field(gt=0, le=720)]
    buffer_before_minutes: Annotated[int, Field(ge=0, le=240)] = 0
    buffer_after_minutes: Annotated[int, Field(ge=0, le=240)] = 0
    price: INRAmount | None = None
    business_timezone: IANATimezone

    @model_validator(mode="after")
    def validate_derived_bounds(self) -> "AppointmentFacts":
        for label, value in (
            ("Appointment start", self.start_at),
            ("Appointment end", self.end_at),
            ("Effective start", self.effective_start_at),
            ("Effective end", self.effective_end_at),
        ):
            validate_business_local(value, self.business_timezone, label=label)
        expected_end = add_elapsed(self.start_at, timedelta(minutes=self.duration_minutes))
        expected_effective_start = add_elapsed(
            self.start_at, -timedelta(minutes=self.buffer_before_minutes)
        )
        expected_effective_end = add_elapsed(
            expected_end, timedelta(minutes=self.buffer_after_minutes)
        )
        if instant(self.end_at) != instant(expected_end):
            raise ValueError("Appointment end does not match duration")
        if instant(self.effective_start_at) != instant(expected_effective_start):
            raise ValueError("Effective start does not match before buffer")
        if instant(self.effective_end_at) != instant(expected_effective_end):
            raise ValueError("Effective end does not match after buffer")
        return self


class CreateAppointmentData(FrozenStrictModel):
    operation: Literal["create"] = "create"
    facts: AppointmentFacts
    customer_name: Annotated[str | None, Field(default=None, max_length=200)]
    customer_phone: E164PhoneNumber
    reason: Annotated[str | None, Field(default=None, max_length=500)]
    call_id: Annotated[PositiveIntegerId | None, Field(default=None)]


class CancelAppointmentData(FrozenStrictModel):
    operation: Literal["cancel"] = "cancel"
    target_appointment_id: PositiveIntegerId
    target_expected_version: PositiveIntegerVersion
    current_facts: AppointmentFacts
    reason_code: Annotated[str | None, Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,49}$")]


class RescheduleAppointmentData(FrozenStrictModel):
    operation: Literal["reschedule"] = "reschedule"
    target_appointment_id: PositiveIntegerId
    target_expected_version: PositiveIntegerVersion
    old_facts: AppointmentFacts
    new_facts: AppointmentFacts

    _SCHEDULING_FIELDS = (
        "service_id",
        "resource_id",
        "start_at",
        "end_at",
        "effective_start_at",
        "effective_end_at",
        "duration_minutes",
        "buffer_before_minutes",
        "buffer_after_minutes",
    )
    _INSTANT_FIELDS = frozenset(("start_at", "end_at", "effective_start_at", "effective_end_at"))

    @model_validator(mode="after")
    def reject_no_op(self) -> "RescheduleAppointmentData":
        old_d = self.old_facts.model_dump(mode="python")
        new_d = self.new_facts.model_dump(mode="python")
        for field in self._SCHEDULING_FIELDS:
            old_v = instant(old_d[field]) if field in self._INSTANT_FIELDS else old_d[field]
            new_v = instant(new_d[field]) if field in self._INSTANT_FIELDS else new_d[field]
            if old_v != new_v:
                return self
        raise ValueError("Reschedule must change at least one scheduling fact")


AppointmentOperationData = Annotated[
    CreateAppointmentData | CancelAppointmentData | RescheduleAppointmentData,
    Field(discriminator="operation"),
]


class PendingAppointmentEnvelope(FrozenStrictModel):
    schema_version: Literal[1] = 1
    action_type: Literal[PendingActionType.APPOINTMENT] = PendingActionType.APPOINTMENT
    data: AppointmentOperationData


class CallbackData(StrictModel):
    """Partial booking facts a human needs to RESUME a booking the caller could
    not finish on a voice call. Carries enough to complete the booking, NOT the
    raw dialogue.

    Tenant identity (business_id) and the authoritative caller identity are bound
    on the PendingAction record from the TRUSTED actor context (business_id
    column, initiated_by), never from this payload — so nothing here is trusted
    for tenant isolation. caller_phone is carried only as the number to dial back
    and is set from the verified session, not a model-extracted value.
    """

    reason_code: Literal[
        "doctor_disambiguation_exhausted",
        "slot_disambiguation_exhausted",
    ]
    caller_phone: E164PhoneNumber
    # Resolved-if-known booking facts. Optional because the give-up happens
    # precisely when something could NOT be resolved — a human resumes from
    # whatever we DID capture, rather than the caller re-explaining everything.
    service_id: Annotated[int | None, Field(default=None, gt=0)]
    service_name: Annotated[str | None, Field(default=None, min_length=1, max_length=200)]
    target_date: ISODate | None = None
    # The candidates we could not disambiguate between (the doctors or slots the
    # caller's words matched more than one of). Bounded; display strings only, no
    # tokens or internal ids beyond what a human needs to pick.
    attempted_candidates: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=200)]],
        Field(default_factory=list, max_length=20),
    ]
    requested_at: AwareDatetime


class PendingCallbackEnvelope(StrictModel):
    schema_version: Literal[1] = 1
    action_type: Literal[PendingActionType.CALLBACK] = PendingActionType.CALLBACK
    data: CallbackData


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


type PayloadEnvelope = (
    PendingOrderEnvelope
    | PendingAppointmentEnvelope
    | OwnerStockUpdateEnvelope
    | PendingCallbackEnvelope
)
type PayloadEnvelopeAdapter = Annotated[PayloadEnvelope, Field(discriminator="action_type")]

_PAYLOAD_REGISTRY: dict[
    tuple[PendingActionType, int],
    type[PendingOrderEnvelope]
    | type[PendingAppointmentEnvelope]
    | type[OwnerStockUpdateEnvelope]
    | type[PendingCallbackEnvelope],
] = {
    (PendingActionType.ORDER, PAYLOAD_SCHEMA_VERSION): PendingOrderEnvelope,
    (PendingActionType.APPOINTMENT, PAYLOAD_SCHEMA_VERSION): PendingAppointmentEnvelope,
    (
        PendingActionType.OWNER_STOCK_UPDATE,
        PAYLOAD_SCHEMA_VERSION,
    ): OwnerStockUpdateEnvelope,
    (PendingActionType.CALLBACK, PAYLOAD_SCHEMA_VERSION): PendingCallbackEnvelope,
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
