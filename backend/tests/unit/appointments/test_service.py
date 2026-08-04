"""Unit tests for D3 appointment create-and-confirm service."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCommand,
    CreatePendingAppointmentCommand,
)
from fonely.domain.appointments.results import PreCommitAppointmentSuccess
from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.payloads import (
    AppointmentFacts,
    CreateAppointmentData,
    PendingAppointmentEnvelope,
)
from fonely.domain.pending_actions.snapshots import canonical_payload_dict
from fonely.models.enums import CallerRole
from fonely.services.appointments import AppointmentService

START = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)


def _actor(business_id: int = 1) -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
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


def _resolved_envelope() -> PendingAppointmentEnvelope:
    return PendingAppointmentEnvelope(
        data=CreateAppointmentData(
            facts=_facts(),
            customer_phone="+919123456789",
        )
    )


def _mock_validation() -> AsyncMock:
    v = AsyncMock()
    v.validate_for_actor.return_value = _resolved_envelope()
    v.validate_stored.return_value = _resolved_envelope()
    return v


async def test_create_proposal_returns_awaiting_confirmation() -> None:
    session = AsyncMock()
    validation = _mock_validation()
    pa_service_mock = AsyncMock()

    create_result = MagicMock()
    create_result.status = "collecting_details"
    create_result.id = 1
    create_result.version = 1

    awaiting_result = MagicMock()
    awaiting_result.id = 1
    awaiting_result.version = 2
    awaiting_result.status = "awaiting_confirmation"
    awaiting_result.expires_at = START + timedelta(hours=1)
    awaiting_result.payload = canonical_payload_dict(_resolved_envelope())

    pa_service_mock.find_idempotent_action.return_value = None
    pa_service_mock.create.return_value = create_result
    pa_service_mock.mark_awaiting_confirmation.return_value = awaiting_result

    service = AppointmentService(session, validation=validation)
    service._pa_service = pa_service_mock

    result = await service.create_proposal(
        CreatePendingAppointmentCommand(
            actor=_actor(),
            service_id=1,
            resource_id=1,
            start_at=START,
            customer_phone="+919123456789",
            expires_at=START + timedelta(hours=1),
            idempotency_key="test-key",
        )
    )

    assert result.status == "awaiting_confirmation"
    assert result.slot_is_held is False
    assert result.pending_action_id == 1
    assert result.version == 2
    assert result.confirmation_facts.operation == "create"
    assert result.confirmation_facts.service_name == "Haircut"


async def test_create_proposal_does_not_commit() -> None:
    session = AsyncMock()
    validation = _mock_validation()
    pa_service_mock = AsyncMock()

    create_result = MagicMock()
    create_result.status = "collecting_details"
    create_result.id = 1
    create_result.version = 1

    awaiting_result = MagicMock()
    awaiting_result.id = 1
    awaiting_result.version = 2
    awaiting_result.status = "awaiting_confirmation"
    awaiting_result.expires_at = START + timedelta(hours=1)
    awaiting_result.payload = canonical_payload_dict(_resolved_envelope())

    pa_service_mock.find_idempotent_action.return_value = None
    pa_service_mock.create.return_value = create_result
    pa_service_mock.mark_awaiting_confirmation.return_value = awaiting_result

    service = AppointmentService(session, validation=validation)
    service._pa_service = pa_service_mock

    await service.create_proposal(
        CreatePendingAppointmentCommand(
            actor=_actor(),
            service_id=1,
            resource_id=1,
            start_at=START,
            customer_phone="+919123456789",
            expires_at=START + timedelta(hours=1),
            idempotency_key="test-key",
        )
    )

    session.commit.assert_not_called()


async def test_proposal_validates_once() -> None:
    session = AsyncMock()
    validation = _mock_validation()
    pa_service_mock = AsyncMock()

    create_result = MagicMock()
    create_result.status = "awaiting_confirmation"
    create_result.id = 1
    create_result.version = 2
    create_result.expires_at = START + timedelta(hours=1)
    create_result.payload = canonical_payload_dict(_resolved_envelope())

    pa_service_mock.find_idempotent_action.return_value = None
    pa_service_mock.create.return_value = create_result

    service = AppointmentService(session, validation=validation)
    service._pa_service = pa_service_mock

    await service.create_proposal(
        CreatePendingAppointmentCommand(
            actor=_actor(),
            service_id=1,
            resource_id=1,
            start_at=START,
            customer_phone="+919123456789",
            expires_at=START + timedelta(hours=1),
            idempotency_key="test-key",
        )
    )

    assert validation.validate_for_actor.call_count == 1


async def test_confirm_replay_returns_authoritative_version() -> None:
    session = AsyncMock()
    validation = _mock_validation()
    service = AppointmentService(session, validation=validation)

    existing = MagicMock()
    existing.id = 42
    existing.pending_action_id = 10
    existing.service_id = 1
    existing.service_name_snapshot = "Haircut"
    existing.resource_id = 1
    existing.resource_name_snapshot = "Priya"
    existing.start_at = START
    existing.end_at = END
    existing.price_snapshot = None
    existing.business_timezone_snapshot = "Asia/Kolkata"

    action = MagicMock()
    action.business_id = 1
    action.version = 5
    action.initiated_by = "+919123456789"
    action.action_type = "appointment"

    service._repo = AsyncMock()
    service._repo.get_by_business_and_pending_action.return_value = existing
    service._pa_service = AsyncMock()
    service._pa_service._require_action = AsyncMock(return_value=action)

    result = await service.confirm_and_commit(
        ConfirmPendingAppointmentCommand(
            actor=_actor(),
            pending_action_id=10,
            expected_version=3,
        )
    )

    assert isinstance(result, PreCommitAppointmentSuccess)
    assert result.appointment.appointment_id == 42
    assert result.pending_action_version == 5
    session.commit.assert_not_called()


async def test_confirm_does_not_call_outer_commit() -> None:
    session = AsyncMock(spec=[])
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    validation = _mock_validation()
    service = AppointmentService(session, validation=validation)

    action = MagicMock()
    action.business_id = 1
    action.version = 2
    action.initiated_by = "+919123456789"
    action.action_type = "appointment"

    begin_result = MagicMock()
    begin_result.version = 3
    begin_result.payload = {
        "schema_version": 1,
        "action_type": "appointment",
        "data": {
            "operation": "create",
            "customer_phone": "+919123456789",
            "facts": {
                "service_id": 1,
                "service_name": "Haircut",
                "resource_id": 1,
                "resource_name": "Priya",
                "start_at": START.isoformat(),
                "end_at": END.isoformat(),
                "effective_start_at": START.isoformat(),
                "effective_end_at": END.isoformat(),
                "duration_minutes": 30,
                "business_timezone": "Asia/Kolkata",
            },
        },
    }

    action.proposed_payload = begin_result.payload

    complete_result = MagicMock()
    complete_result.version = 4

    service._pa_service = AsyncMock()
    service._pa_service._require_action = AsyncMock(return_value=action)
    service._pa_service.begin_commit = AsyncMock(return_value=begin_result)
    service._pa_service.complete_commit = AsyncMock(return_value=complete_result)

    service._repo = AsyncMock()
    service._repo.get_by_business_and_pending_action.return_value = None

    mock_appointment = MagicMock()
    mock_appointment.id = 1
    service._repo.insert.return_value = mock_appointment

    @asynccontextmanager
    async def _fake_nested():  # type: ignore[no-untyped-def]
        yield

    session.begin_nested = _fake_nested

    result = await service.confirm_and_commit(
        ConfirmPendingAppointmentCommand(
            actor=_actor(),
            pending_action_id=10,
            expected_version=2,
        )
    )

    assert isinstance(result, PreCommitAppointmentSuccess)
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
