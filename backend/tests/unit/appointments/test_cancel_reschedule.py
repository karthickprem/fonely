"""Unit tests for appointment cancellation and rescheduling service."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from fonely.domain.appointments.commands import (
    CreatePendingAppointmentCancellationCommand,
    CreatePendingAppointmentRescheduleCommand,
)
from fonely.domain.appointments.errors import AppointmentDomainError, AppointmentErrorCode
from fonely.domain.appointments.results import (
    AppointmentProposalResult,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.payloads import (
    AppointmentFacts,
    PendingAppointmentEnvelope,
    RescheduleAppointmentData,
)
from fonely.models.enums import CallerRole, Channel
from fonely.services.appointments import AppointmentService

START = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)
NEW_START = datetime(2026, 8, 5, 14, 0, tzinfo=UTC)
NEW_END = NEW_START + timedelta(minutes=30)


def _actor(business_id: int = 1) -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
    )


def _facts() -> AppointmentFacts:
    return AppointmentFacts(
        service_id=1,
        service_name="Haircut",
        resource_id=1,
        resource_name="Priya",
        start_at=START,
        end_at=END,
        effective_start_at=START,
        effective_end_at=END,
        duration_minutes=30,
        business_timezone="Asia/Kolkata",
    )


def _new_facts() -> AppointmentFacts:
    return AppointmentFacts(
        service_id=1,
        service_name="Haircut",
        resource_id=1,
        resource_name="Priya",
        start_at=NEW_START,
        end_at=NEW_END,
        effective_start_at=NEW_START,
        effective_end_at=NEW_END,
        duration_minutes=30,
        business_timezone="Asia/Kolkata",
    )


def _mock_appointment(
    *,
    status: str = "confirmed",
    version: int = 1,
    appointment_id: int = 10,
) -> MagicMock:
    appt = MagicMock()
    appt.id = appointment_id
    appt.business_id = 1
    appt.service_id = 1
    appt.resource_id = 1
    appt.status = status
    appt.version = version
    appt.start_at = START
    appt.end_at = END
    appt.effective_start_at = START
    appt.effective_end_at = END
    appt.service_name_snapshot = "Haircut"
    appt.resource_name_snapshot = "Priya"
    appt.duration_minutes_snapshot = 30
    appt.buffer_before_minutes_snapshot = 0
    appt.buffer_after_minutes_snapshot = 0
    appt.price_snapshot = None
    appt.business_timezone_snapshot = "Asia/Kolkata"
    appt.pending_action_id = 1
    appt.customer_phone = "+919123456789"
    appt.customer_name = None
    appt.call_id = None
    appt.reason = None
    appt.source = "customer_conversation"
    appt.idempotency_key = "pa-1"
    appt.cancelled_at = None
    appt.rescheduled_at = None
    appt.created_at = START - timedelta(hours=1)
    appt.updated_at = START - timedelta(hours=1)
    return appt


def _mock_session() -> AsyncMock:
    session = AsyncMock()

    @asynccontextmanager
    async def _nested():
        yield

    session.begin_nested = _nested
    return session


def _mock_validation() -> AsyncMock:
    v = AsyncMock()

    async def validate_for_actor(actor, envelope):
        data = envelope.data
        if isinstance(data, RescheduleAppointmentData):
            return PendingAppointmentEnvelope(
                data=RescheduleAppointmentData(
                    target_appointment_id=data.target_appointment_id,
                    target_expected_version=data.target_expected_version,
                    old_facts=data.old_facts,
                    new_facts=_new_facts(),
                )
            )
        return envelope

    v.validate_for_actor = validate_for_actor
    return v


# --- Cancellation proposal tests ---


class TestCancellationProposal:
    @pytest.mark.asyncio
    async def test_creates_cancellation_proposal_for_confirmed_appointment(self) -> None:
        session = _mock_session()
        validation = _mock_validation()
        service = AppointmentService(session, validation=validation)
        service._pa_service.find_idempotent_action = AsyncMock(return_value=None)

        appt = _mock_appointment()
        service._repo.get_by_business_and_id = AsyncMock(return_value=appt)

        pa_result = MagicMock()
        pa_result.id = 100
        pa_result.version = 2
        pa_result.status = "awaiting_confirmation"
        pa_result.expires_at = START + timedelta(minutes=15)
        pa_result.payload = {}
        service._pa_service.create = AsyncMock(return_value=pa_result)
        service._pa_service.mark_awaiting_confirmation = AsyncMock(return_value=pa_result)

        result = await service.create_cancellation_proposal(
            CreatePendingAppointmentCancellationCommand(
                actor=_actor(),
                appointment_id=10,
                expected_appointment_version=1,
                expires_at=START + timedelta(minutes=15),
                idempotency_key="cancel-1",
            )
        )

        assert isinstance(result, AppointmentProposalResult)
        assert result.pending_action_id == 100
        assert result.confirmation_facts.operation == "cancel"
        assert result.confirmation_facts.target_appointment_id == 10

    @pytest.mark.asyncio
    async def test_rejects_cancellation_of_already_cancelled(self) -> None:
        session = _mock_session()
        validation = _mock_validation()
        service = AppointmentService(session, validation=validation)
        service._pa_service.find_idempotent_action = AsyncMock(return_value=None)

        appt = _mock_appointment(status="cancelled")
        service._repo.get_by_business_and_id = AsyncMock(return_value=appt)

        with pytest.raises(AppointmentDomainError) as exc_info:
            await service.create_cancellation_proposal(
                CreatePendingAppointmentCancellationCommand(
                    actor=_actor(),
                    appointment_id=10,
                    expected_appointment_version=1,
                    expires_at=START + timedelta(minutes=15),
                    idempotency_key="cancel-2",
                )
            )
        assert exc_info.value.code == AppointmentErrorCode.INVALID_STATE

    @pytest.mark.asyncio
    async def test_rejects_cancellation_of_completed(self) -> None:
        session = _mock_session()
        validation = _mock_validation()
        service = AppointmentService(session, validation=validation)
        service._pa_service.find_idempotent_action = AsyncMock(return_value=None)

        appt = _mock_appointment(status="completed")
        service._repo.get_by_business_and_id = AsyncMock(return_value=appt)

        with pytest.raises(AppointmentDomainError) as exc_info:
            await service.create_cancellation_proposal(
                CreatePendingAppointmentCancellationCommand(
                    actor=_actor(),
                    appointment_id=10,
                    expected_appointment_version=1,
                    expires_at=START + timedelta(minutes=15),
                    idempotency_key="cancel-3",
                )
            )
        assert exc_info.value.code == AppointmentErrorCode.INVALID_STATE

    @pytest.mark.asyncio
    async def test_rejects_cancellation_of_nonexistent(self) -> None:
        session = _mock_session()
        validation = _mock_validation()
        service = AppointmentService(session, validation=validation)
        service._pa_service.find_idempotent_action = AsyncMock(return_value=None)
        service._repo.get_by_business_and_id = AsyncMock(return_value=None)

        with pytest.raises(AppointmentDomainError) as exc_info:
            await service.create_cancellation_proposal(
                CreatePendingAppointmentCancellationCommand(
                    actor=_actor(),
                    appointment_id=99,
                    expected_appointment_version=1,
                    expires_at=START + timedelta(minutes=15),
                    idempotency_key="cancel-4",
                )
            )
        assert exc_info.value.code == AppointmentErrorCode.NOT_FOUND


# --- Reschedule proposal tests ---


class TestRescheduleProposal:
    @pytest.mark.asyncio
    async def test_creates_reschedule_proposal_for_confirmed(self) -> None:
        session = _mock_session()
        validation = _mock_validation()
        service = AppointmentService(session, validation=validation)
        service._pa_service.find_idempotent_action = AsyncMock(return_value=None)

        appt = _mock_appointment()
        service._repo.get_by_business_and_id = AsyncMock(return_value=appt)

        pa_result = MagicMock()
        pa_result.id = 200
        pa_result.version = 2
        pa_result.status = "awaiting_confirmation"
        pa_result.expires_at = START + timedelta(minutes=15)
        pa_result.payload = {}
        service._pa_service.create = AsyncMock(return_value=pa_result)
        service._pa_service.mark_awaiting_confirmation = AsyncMock(return_value=pa_result)

        result = await service.create_reschedule_proposal(
            CreatePendingAppointmentRescheduleCommand(
                actor=_actor(),
                appointment_id=10,
                expected_appointment_version=1,
                service_id=1,
                start_at=NEW_START,
                expires_at=START + timedelta(minutes=15),
                idempotency_key="resched-1",
            )
        )

        assert isinstance(result, AppointmentProposalResult)
        assert result.pending_action_id == 200
        assert result.confirmation_facts.operation == "reschedule"
        assert result.confirmation_facts.target_appointment_id == 10
        assert result.confirmation_facts.old_facts is not None

    @pytest.mark.asyncio
    async def test_rejects_reschedule_of_cancelled(self) -> None:
        session = _mock_session()
        validation = _mock_validation()
        service = AppointmentService(session, validation=validation)
        service._pa_service.find_idempotent_action = AsyncMock(return_value=None)

        appt = _mock_appointment(status="cancelled")
        service._repo.get_by_business_and_id = AsyncMock(return_value=appt)

        with pytest.raises(AppointmentDomainError) as exc_info:
            await service.create_reschedule_proposal(
                CreatePendingAppointmentRescheduleCommand(
                    actor=_actor(),
                    appointment_id=10,
                    expected_appointment_version=1,
                    service_id=1,
                    start_at=NEW_START,
                    expires_at=START + timedelta(minutes=15),
                    idempotency_key="resched-2",
                )
            )
        assert exc_info.value.code == AppointmentErrorCode.INVALID_STATE

    @pytest.mark.asyncio
    async def test_rejects_reschedule_of_nonexistent(self) -> None:
        session = _mock_session()
        validation = _mock_validation()
        service = AppointmentService(session, validation=validation)
        service._pa_service.find_idempotent_action = AsyncMock(return_value=None)
        service._repo.get_by_business_and_id = AsyncMock(return_value=None)

        with pytest.raises(AppointmentDomainError) as exc_info:
            await service.create_reschedule_proposal(
                CreatePendingAppointmentRescheduleCommand(
                    actor=_actor(),
                    appointment_id=99,
                    expected_appointment_version=1,
                    service_id=1,
                    start_at=NEW_START,
                    expires_at=START + timedelta(minutes=15),
                    idempotency_key="resched-3",
                )
            )
        assert exc_info.value.code == AppointmentErrorCode.NOT_FOUND


# --- Tenant isolation ---


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_cancellation_uses_business_scoped_lookup(self) -> None:
        session = _mock_session()
        validation = _mock_validation()
        service = AppointmentService(session, validation=validation)
        service._pa_service.find_idempotent_action = AsyncMock(return_value=None)
        service._repo.get_by_business_and_id = AsyncMock(return_value=None)

        with pytest.raises(AppointmentDomainError):
            await service.create_cancellation_proposal(
                CreatePendingAppointmentCancellationCommand(
                    actor=_actor(business_id=2),
                    appointment_id=10,
                    expected_appointment_version=1,
                    expires_at=START + timedelta(minutes=15),
                    idempotency_key="tenant-1",
                )
            )

        service._repo.get_by_business_and_id.assert_awaited_once_with(2, 10)
