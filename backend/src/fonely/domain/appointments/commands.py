"""Strict commands and queries for the generic appointment capability."""

from datetime import date, time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fonely.core.validators import (
    AwareDatetime,
    E164PhoneNumber,
    PositiveIntegerId,
    PositiveIntegerVersion,
)
from fonely.domain.appointments.datetimes import is_before
from fonely.domain.pending_actions.commands import ActorContext


class AppointmentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CheckAvailabilityQuery(AppointmentCommand):
    actor: ActorContext
    service_id: PositiveIntegerId
    local_date: date
    earliest_local_time: time | None = None
    latest_local_time: time | None = None
    resource_id: Annotated[PositiveIntegerId | None, Field(default=None)]
    result_limit: Annotated[int, Field(ge=1, le=10)] = 3

    @model_validator(mode="after")
    def validate_local_time_range(self) -> "CheckAvailabilityQuery":
        for value in (self.earliest_local_time, self.latest_local_time):
            if value is not None and value.tzinfo is not None:
                raise ValueError("Availability times must be naive local wall times")
        if (
            self.earliest_local_time is not None
            and self.latest_local_time is not None
            and self.latest_local_time <= self.earliest_local_time
        ):
            raise ValueError("Latest local time must be after earliest local time")
        return self


class CreatePendingAppointmentCommand(AppointmentCommand):
    actor: ActorContext
    service_id: PositiveIntegerId
    resource_id: Annotated[PositiveIntegerId | None, Field(default=None)]
    start_at: AwareDatetime
    customer_name: Annotated[str | None, Field(default=None, max_length=200)]
    customer_phone: E164PhoneNumber
    reason: Annotated[str | None, Field(default=None, max_length=500)]
    call_id: Annotated[PositiveIntegerId | None, Field(default=None)]
    expires_at: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]


class _ConfirmAppointmentCommand(AppointmentCommand):
    actor: ActorContext
    pending_action_id: PositiveIntegerId
    expected_version: PositiveIntegerVersion


class ConfirmPendingAppointmentCommand(_ConfirmAppointmentCommand):
    pass


class CreatePendingAppointmentCancellationCommand(AppointmentCommand):
    actor: ActorContext
    appointment_id: PositiveIntegerId
    expected_appointment_version: PositiveIntegerVersion
    reason_code: Annotated[str | None, Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,49}$")]
    expires_at: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]


class ConfirmPendingAppointmentCancellationCommand(_ConfirmAppointmentCommand):
    pass


class CreatePendingAppointmentRescheduleCommand(AppointmentCommand):
    actor: ActorContext
    appointment_id: PositiveIntegerId
    expected_appointment_version: PositiveIntegerVersion
    service_id: PositiveIntegerId
    resource_id: Annotated[PositiveIntegerId | None, Field(default=None)]
    start_at: AwareDatetime
    expires_at: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]


class ConfirmPendingAppointmentRescheduleCommand(_ConfirmAppointmentCommand):
    pass


class GetAppointmentQuery(AppointmentCommand):
    actor: ActorContext
    appointment_id: PositiveIntegerId


class ListCustomerAppointmentsQuery(AppointmentCommand):
    actor: ActorContext
    customer_phone: E164PhoneNumber
    include_past: bool = False
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class RecordExternalAppointmentCommand(AppointmentCommand):
    actor: ActorContext
    service_id: PositiveIntegerId
    resource_id: PositiveIntegerId
    start_at: AwareDatetime
    customer_name: Annotated[str | None, Field(default=None, max_length=200)]
    customer_phone: E164PhoneNumber
    source: Literal["owner_manual", "walk_in"]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]


class BlockResourceTimeCommand(AppointmentCommand):
    actor: ActorContext
    resource_id: PositiveIntegerId
    effective_start_at: AwareDatetime
    effective_end_at: AwareDatetime
    reason: Annotated[str | None, Field(default=None, max_length=500)]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def validate_effective_interval(self) -> "BlockResourceTimeCommand":
        if not is_before(self.effective_start_at, self.effective_end_at):
            raise ValueError("Resource block end must be after start")
        return self


class UnblockResourceTimeCommand(AppointmentCommand):
    actor: ActorContext
    allocation_id: PositiveIntegerId
    expected_version: PositiveIntegerVersion
