"""Internal appointment slice request/response models."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

StrictPositiveId = Annotated[int, Field(gt=0, le=2_147_483_647, strict=True)]


class AppointmentProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: StrictPositiveId
    resource_id: StrictPositiveId
    start_at: datetime
    customer_name: str | None = Field(default=None, max_length=200)
    customer_phone: str = Field(min_length=1, max_length=20)
    reason: str | None = Field(default=None, max_length=500)
    call_id: Annotated[int | None, Field(default=None, gt=0, le=2_147_483_647, strict=True)]
    idempotency_key: str = Field(min_length=1, max_length=100)
    expires_at: datetime

    @field_validator("start_at", "expires_at")
    @classmethod
    def require_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("Datetime must be timezone-aware")
        return v

    @field_validator("customer_phone")
    @classmethod
    def validate_phone_format(cls, v: str) -> str:
        if not v.startswith("+") or not v[1:].isdigit() or len(v) < 8:
            raise ValueError("Phone must be E.164 format")
        return v


class AppointmentConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: StrictPositiveId


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
