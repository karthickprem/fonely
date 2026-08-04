"""Immutable safe results for the generic appointment capability."""

import enum
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, computed_field, model_validator

from fonely.core.validators import (
    AwareDatetime,
    E164PhoneNumber,
    IANATimezone,
    INRAmount,
    PositiveIntegerId,
    PositiveIntegerVersion,
)
from fonely.domain.appointments.datetimes import add_elapsed, instant


def _require_datetime(value: object) -> object:
    if not isinstance(value, datetime):
        raise ValueError("Result timestamp must be a datetime object")
    return value


ResultDatetime = Annotated[AwareDatetime, BeforeValidator(_require_datetime)]
PositiveId = PositiveIntegerId
PositiveVersion = PositiveIntegerVersion
SnapshotName = Annotated[str, Field(min_length=1, max_length=200)]


class ConfirmationFactsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation: Literal["create", "cancel", "reschedule"]
    service_id: PositiveId
    service_name: SnapshotName
    resource_id: PositiveId
    resource_name: SnapshotName
    start_at: str
    end_at: str
    duration_minutes: Annotated[int, Field(gt=0, le=720)]
    price: str | None = None
    business_timezone: IANATimezone
    target_appointment_id: PositiveId | None = None
    reason_code: Annotated[str | None, Field(default=None, max_length=50)] = None
    old_facts: "ConfirmationSchedulingFacts | None" = None


class ConfirmationSchedulingFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    service_id: PositiveId
    service_name: SnapshotName
    resource_id: PositiveId
    resource_name: SnapshotName
    start_at: str
    end_at: str
    duration_minutes: Annotated[int, Field(gt=0, le=720)]
    price: str | None = None
    business_timezone: IANATimezone


class AppointmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AvailabilitySlot(AppointmentResult):
    service_id: PositiveId
    service_name: SnapshotName
    resource_id: PositiveId
    resource_name: SnapshotName
    start_at: ResultDatetime
    end_at: ResultDatetime
    effective_start_at: ResultDatetime
    effective_end_at: ResultDatetime
    duration_minutes: Annotated[int, Field(gt=0, le=720)]
    buffer_before_minutes: Annotated[int, Field(ge=0, le=240)]
    buffer_after_minutes: Annotated[int, Field(ge=0, le=240)]
    business_timezone: IANATimezone

    @model_validator(mode="after")
    def validate_bounds(self) -> "AvailabilitySlot":
        expected_end = add_elapsed(self.start_at, timedelta(minutes=self.duration_minutes))
        expected_effective_start = add_elapsed(
            self.start_at, -timedelta(minutes=self.buffer_before_minutes)
        )
        expected_effective_end = add_elapsed(
            expected_end, timedelta(minutes=self.buffer_after_minutes)
        )
        if instant(self.end_at) != instant(expected_end):
            raise ValueError("Availability end does not match duration")
        if instant(self.effective_start_at) != instant(expected_effective_start):
            raise ValueError("Availability effective start does not match before buffer")
        if instant(self.effective_end_at) != instant(expected_effective_end):
            raise ValueError("Availability effective end does not match after buffer")
        return self


class AvailabilityResult(AppointmentResult):
    slots: tuple[AvailabilitySlot, ...]


class AppointmentProposalResult(AppointmentResult):
    pending_action_id: PositiveId
    version: PositiveVersion
    status: str = "awaiting_confirmation"
    slot_is_held: Literal[False] = False
    expires_at: ResultDatetime
    confirmation_facts: ConfirmationFactsResult


class AppointmentConfirmationResult(AppointmentResult):
    appointment_id: PositiveId
    pending_action_id: PositiveId
    service_id: PositiveId
    service_name: SnapshotName
    resource_id: PositiveId
    resource_name: SnapshotName
    start_at: ResultDatetime
    end_at: ResultDatetime
    price: INRAmount | None
    business_timezone: IANATimezone

    @model_validator(mode="after")
    def validate_interval(self) -> "AppointmentConfirmationResult":
        if instant(self.end_at) <= instant(self.start_at):
            raise ValueError("Appointment end must be after start")
        return self


class AppointmentCommitFailureCode(enum.StrEnum):
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    REVALIDATION_REQUIRED = "revalidation_required"
    TRANSACTION_FAILED = "transaction_failed"


class PreCommitAppointmentSuccess(AppointmentResult):
    outcome: Literal["success"] = "success"
    appointment: AppointmentConfirmationResult
    pending_action_version: PositiveVersion


class PreCommitAppointmentFailure(AppointmentResult):
    """Expected recoverable failure whose fail_commit state may be committed.

    Unexpected exceptions are not represented by this type; they escape and roll
    back the caller-owned outer transaction.
    """

    outcome: Literal["failure"] = "failure"
    pending_action_id: PositiveId
    pending_action_version: PositiveVersion
    error_code: AppointmentCommitFailureCode

    @computed_field
    def retryable(self) -> Literal[True]:
        return True


type PreCommitAppointmentOutcome = PreCommitAppointmentSuccess | PreCommitAppointmentFailure


class AppointmentCancellationResult(AppointmentResult):
    appointment_id: PositiveId
    appointment_commit_id: PositiveId
    status: Literal["cancelled"] = "cancelled"
    cancelled_at: ResultDatetime


class AppointmentRescheduleResult(AppointmentResult):
    appointment_id: PositiveId
    appointment_commit_id: PositiveId
    version: PositiveVersion
    resource_id: PositiveId
    resource_name: SnapshotName
    start_at: ResultDatetime
    end_at: ResultDatetime

    @model_validator(mode="after")
    def validate_interval(self) -> "AppointmentRescheduleResult":
        if instant(self.end_at) <= instant(self.start_at):
            raise ValueError("Appointment end must be after start")
        return self


class ExternalAppointmentResult(AppointmentResult):
    appointment_id: PositiveId
    allocation_id: PositiveId
    source: Literal["owner_manual", "walk_in"]


class ResourceBlockResult(AppointmentResult):
    allocation_id: PositiveId
    resource_id: PositiveId
    effective_start_at: ResultDatetime
    effective_end_at: ResultDatetime
    status: Literal["active", "released", "cancelled"]

    @model_validator(mode="after")
    def validate_interval(self) -> "ResourceBlockResult":
        if instant(self.effective_end_at) <= instant(self.effective_start_at):
            raise ValueError("Resource block end must be after start")
        return self


class AppointmentLookupResult(AppointmentResult):
    appointment_id: PositiveId
    version: PositiveVersion
    customer_phone: E164PhoneNumber
    service_id: PositiveId
    service_name: SnapshotName
    resource_id: PositiveId
    resource_name: SnapshotName
    start_at: ResultDatetime
    end_at: ResultDatetime
    status: Literal["confirmed", "completed", "cancelled", "no_show"]

    @model_validator(mode="after")
    def validate_interval(self) -> "AppointmentLookupResult":
        if instant(self.end_at) <= instant(self.start_at):
            raise ValueError("Appointment end must be after start")
        return self
