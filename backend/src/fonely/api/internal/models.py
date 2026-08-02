"""Internal appointment slice request/response models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AppointmentProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: int = Field(gt=0)
    resource_id: int | None = Field(default=None, gt=0)
    start_at: datetime
    customer_name: str | None = Field(default=None, max_length=200)
    customer_phone: str = Field(min_length=1, max_length=20)
    reason: str | None = Field(default=None, max_length=500)
    call_id: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=1, max_length=100)
    expires_at: datetime


class AppointmentConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)


class ProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    status: str
    pending_action_id: int
    version: int
    expires_at: datetime
    slot_is_held: bool
    confirmation_facts: dict[str, object]


class CommittedAppointmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    status: str = "committed"
    appointment_id: int
    pending_action_id: int
    service_name: str
    resource_name: str
    start_at: datetime
    end_at: datetime
    business_timezone: str


class RetryableFailureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    status: str = "retryable_failure"
    error_code: str
    retryable: bool = True
    pending_action_id: int
    pending_action_version: int


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    status: str = "error"
    error_code: str
    message: str
