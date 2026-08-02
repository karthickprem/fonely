"""Unit tests for internal appointment request/response models."""

import pytest
from pydantic import ValidationError

from fonely.api.internal.models import (
    AppointmentConfirmRequest,
    AppointmentProposalRequest,
    ErrorResponse,
)


def test_proposal_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AppointmentProposalRequest(
            service_id=1,
            resource_id=1,
            start_at="2026-08-05T10:00:00Z",
            customer_phone="+919123456789",
            idempotency_key="test",
            expires_at="2026-08-05T11:00:00Z",
            business_id=1,  # type: ignore[call-arg]
        )


def test_proposal_request_rejects_nonpositive_service() -> None:
    with pytest.raises(ValidationError):
        AppointmentProposalRequest(
            service_id=0,
            resource_id=1,
            start_at="2026-08-05T10:00:00Z",
            customer_phone="+919123456789",
            idempotency_key="test",
            expires_at="2026-08-05T11:00:00Z",
        )


def test_proposal_request_rejects_overflow_id() -> None:
    with pytest.raises(ValidationError):
        AppointmentProposalRequest(
            service_id=2_147_483_648,
            resource_id=1,
            start_at="2026-08-05T10:00:00Z",
            customer_phone="+919123456789",
            idempotency_key="test",
            expires_at="2026-08-05T11:00:00Z",
        )


def test_proposal_request_requires_resource_id() -> None:
    with pytest.raises(ValidationError):
        AppointmentProposalRequest(
            service_id=1,
            start_at="2026-08-05T10:00:00Z",
            customer_phone="+919123456789",
            idempotency_key="test",
            expires_at="2026-08-05T11:00:00Z",
        )


def test_proposal_request_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        AppointmentProposalRequest(
            service_id=1,
            resource_id=1,
            start_at="2026-08-05T10:00:00",
            customer_phone="+919123456789",
            idempotency_key="test",
            expires_at="2026-08-05T11:00:00Z",
        )


def test_proposal_request_rejects_invalid_phone() -> None:
    with pytest.raises(ValidationError):
        AppointmentProposalRequest(
            service_id=1,
            resource_id=1,
            start_at="2026-08-05T10:00:00Z",
            customer_phone="12345",
            idempotency_key="test",
            expires_at="2026-08-05T11:00:00Z",
        )


def test_confirm_request_requires_version() -> None:
    with pytest.raises(ValidationError):
        AppointmentConfirmRequest()  # type: ignore[call-arg]


def test_confirm_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AppointmentConfirmRequest(
            expected_version=1,
            actor_phone="+91xxx",  # type: ignore[call-arg]
        )


def test_proposal_request_rejects_string_service_id() -> None:
    with pytest.raises(ValidationError):
        AppointmentProposalRequest(
            service_id="1",  # type: ignore[arg-type]
            resource_id=1,
            start_at="2026-08-05T10:00:00Z",
            customer_phone="+919123456789",
            idempotency_key="test",
            expires_at="2026-08-05T11:00:00Z",
        )


def test_proposal_request_rejects_boolean_service_id() -> None:
    with pytest.raises(ValidationError):
        AppointmentProposalRequest(
            service_id=True,  # type: ignore[arg-type]
            resource_id=1,
            start_at="2026-08-05T10:00:00Z",
            customer_phone="+919123456789",
            idempotency_key="test",
            expires_at="2026-08-05T11:00:00Z",
        )


def test_proposal_request_rejects_float_service_id() -> None:
    with pytest.raises(ValidationError):
        AppointmentProposalRequest(
            service_id=1.0,  # type: ignore[arg-type]
            resource_id=1,
            start_at="2026-08-05T10:00:00Z",
            customer_phone="+919123456789",
            idempotency_key="test",
            expires_at="2026-08-05T11:00:00Z",
        )


def test_proposal_request_accepts_valid_request() -> None:
    req = AppointmentProposalRequest(
        service_id=1,
        resource_id=1,
        start_at="2026-08-05T10:00:00Z",
        customer_phone="+919123456789",
        idempotency_key="test",
        expires_at="2026-08-05T11:00:00Z",
    )
    assert req.service_id == 1


def test_error_response_shape() -> None:
    resp = ErrorResponse(
        correlation_id="abc",
        error_code="not_found",
        message="Gone",
    )
    assert resp.status == "error"
    assert resp.correlation_id == "abc"
