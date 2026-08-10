"""PostgreSQL contracts for D3 appointment create-and-confirm transaction.

Collected everywhere; executed only against the guarded local PostgreSQL test
database configured by the integration-test fixtures.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.core.validators import utcnow
from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCommand,
    CreatePendingAppointmentCommand,
)
from fonely.domain.appointments.results import (
    PreCommitAppointmentFailure,
    PreCommitAppointmentSuccess,
)
from fonely.domain.appointments.validation import AppointmentValidationPort
from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.errors import PendingActionUnauthorizedError
from fonely.domain.pending_actions.payloads import (
    AppointmentFacts,
    CreateAppointmentData,
    PendingAppointmentEnvelope,
)
from fonely.models.enums import CallerRole, PendingActionType
from fonely.models.schema import Appointment, PendingAction, ResourceAllocation
from fonely.services.appointments import AppointmentService

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import notifications, whatsapp_config

    mappings = '{"phone-1": 1}'
    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_phone_number_id", "phone-1")


def _slot_start() -> datetime:
    now = utcnow()
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)


START = _slot_start()
END = START + timedelta(minutes=30)


def _actor(business_id: int = 1, phone: str = "+919123456789") -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone=phone,
        verified_role=CallerRole.CUSTOMER,
    )


def _facts(
    *,
    start_at: datetime = START,
    resource_id: int = 1,
    price: str | None = "500.00",
) -> AppointmentFacts:
    end_at = start_at + timedelta(minutes=30)
    return AppointmentFacts(
        service_id=1,
        service_name="Haircut",
        resource_id=resource_id,
        resource_name="Priya",
        start_at=start_at,
        end_at=end_at,
        effective_start_at=start_at,
        effective_end_at=end_at,
        duration_minutes=30,
        price=price,
        business_timezone="Asia/Kolkata",
    )


class StubValidationPort:
    def __init__(self, facts: AppointmentFacts | None = None) -> None:
        self._facts = facts or _facts()

    async def validate_for_actor(
        self,
        actor: ActorContext,
        payload: PendingAppointmentEnvelope,
    ) -> PendingAppointmentEnvelope:
        assert isinstance(payload.data, CreateAppointmentData)
        return PendingAppointmentEnvelope(
            data=CreateAppointmentData(
                facts=self._facts,
                customer_name=payload.data.customer_name,
                customer_phone=payload.data.customer_phone,
                reason=payload.data.reason,
                call_id=payload.data.call_id,
            )
        )

    async def validate_stored(
        self,
        business_id: int,
        payload: PendingAppointmentEnvelope,
    ) -> PendingAppointmentEnvelope:
        return payload

    async def validate_idempotent_retry(
        self,
        actor: ActorContext,
        proposed: PendingAppointmentEnvelope,
        stored: PendingAppointmentEnvelope,
    ) -> None:
        pass

    async def validate_completion_evidence(
        self,
        business_id: int,
        payload: PendingAppointmentEnvelope,
        committed_entity_type: str,
        committed_entity_id: int,
    ) -> None:
        pass


async def _seed_catalog(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Salon', 'salon', '+919000000001', 'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (1, '+919000000001', 'owner', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'Haircut', 30, 0, 0, 500.00, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Priya', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) "
            "VALUES (1, 1, 1, true)"
        )
    )
    await session.flush()


async def _create_and_confirm(
    session: AsyncSession,
    *,
    validation: AppointmentValidationPort | None = None,
    idempotency_key: str = "test-appt-1",
    start_at: datetime = START,
    actor: ActorContext | None = None,
) -> PreCommitAppointmentSuccess | PreCommitAppointmentFailure:
    a = actor or _actor()
    v = validation or StubValidationPort(facts=_facts(start_at=start_at))
    service = AppointmentService(session, validation=v)

    proposal = await service.create_proposal(
        CreatePendingAppointmentCommand(
            actor=a,
            service_id=1,
            resource_id=1,
            start_at=start_at,
            customer_phone=a.normalized_phone,
            expires_at=utcnow() + timedelta(minutes=15),
            idempotency_key=idempotency_key,
        )
    )

    result = await service.confirm_and_commit(
        ConfirmPendingAppointmentCommand(
            actor=a,
            pending_action_id=proposal.pending_action_id,
            expected_version=proposal.version,
        )
    )
    return result


async def test_proposal_creates_pending_action_without_appointment(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        service = AppointmentService(session, validation=StubValidationPort())

        proposal = await service.create_proposal(
            CreatePendingAppointmentCommand(
                actor=_actor(),
                service_id=1,
                resource_id=1,
                start_at=START,
                customer_phone="+919123456789",
                expires_at=utcnow() + timedelta(minutes=15),
                idempotency_key="proposal-only",
            )
        )

        assert proposal.status == "awaiting_confirmation"
        assert proposal.slot_is_held is False

        appt_count = await session.scalar(select(func.count(Appointment.id)))
        alloc_count = await session.scalar(select(func.count(ResourceAllocation.id)))
        pa_count = await session.scalar(
            select(func.count(PendingAction.id)).where(
                PendingAction.action_type == PendingActionType.APPOINTMENT.value
            )
        )

        assert appt_count == 0
        assert alloc_count == 0
        assert pa_count == 1
        await session.rollback()


async def test_successful_confirmation_creates_appointment_and_allocation(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        result = await _create_and_confirm(session)

        assert isinstance(result, PreCommitAppointmentSuccess)
        assert result.appointment.service_name == "Haircut"
        assert result.appointment.resource_name == "Priya"

        appt = (
            await session.execute(
                select(Appointment).where(Appointment.id == result.appointment.appointment_id)
            )
        ).scalar_one()
        assert appt.business_id == 1
        assert appt.source == "customer_conversation"
        assert appt.status == "confirmed"
        assert appt.pending_action_id is not None

        alloc = (
            await session.execute(
                select(ResourceAllocation).where(ResourceAllocation.appointment_id == appt.id)
            )
        ).scalar_one()
        assert alloc.business_id == 1
        assert alloc.resource_id == 1
        assert alloc.status == "active"
        assert alloc.allocation_type == "appointment"
        assert alloc.source == "customer_conversation"
        assert alloc.pending_action_id == appt.pending_action_id

        pa = (
            await session.execute(
                select(PendingAction).where(PendingAction.id == appt.pending_action_id)
            )
        ).scalar_one()
        assert pa.status == "confirmed"
        assert pa.committed_entity_type == "appointment"
        assert pa.committed_entity_id == appt.id
        await session.rollback()


async def test_outer_rollback_leaves_no_rows(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        result = await _create_and_confirm(session)
        assert isinstance(result, PreCommitAppointmentSuccess)
        await session.rollback()

    async with pg_session_factory() as session:
        appt_count = await session.scalar(select(func.count(Appointment.id)))
        alloc_count = await session.scalar(select(func.count(ResourceAllocation.id)))
        pa_count = await session.scalar(select(func.count(PendingAction.id)))
        assert appt_count == 0
        assert alloc_count == 0
        assert pa_count == 0


async def test_replay_returns_authoritative_version(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        result1 = await _create_and_confirm(session)
        assert isinstance(result1, PreCommitAppointmentSuccess)
        pa_id = result1.appointment.pending_action_id
        await session.commit()

    async with pg_session_factory() as session:
        service = AppointmentService(session, validation=StubValidationPort())
        result2 = await service.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=_actor(),
                pending_action_id=pa_id,
                expected_version=999,
            )
        )
        assert isinstance(result2, PreCommitAppointmentSuccess)
        assert result2.appointment.appointment_id == (result1.appointment.appointment_id)
        assert result2.pending_action_version == result1.pending_action_version
        await session.rollback()


async def test_unrelated_customer_cannot_confirm(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        service = AppointmentService(session, validation=StubValidationPort())
        proposal = await service.create_proposal(
            CreatePendingAppointmentCommand(
                actor=_actor(),
                service_id=1,
                resource_id=1,
                start_at=START,
                customer_phone="+919123456789",
                expires_at=utcnow() + timedelta(minutes=15),
                idempotency_key="auth-test",
            )
        )

        unrelated = _actor(phone="+919999999999")
        with pytest.raises(PendingActionUnauthorizedError):
            await service.confirm_and_commit(
                ConfirmPendingAppointmentCommand(
                    actor=unrelated,
                    pending_action_id=proposal.pending_action_id,
                    expected_version=proposal.version,
                )
            )

        appt_count = await session.scalar(select(func.count(Appointment.id)))
        assert appt_count == 0
        await session.rollback()


async def test_cross_tenant_confirm_forbidden(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        service = AppointmentService(session, validation=StubValidationPort())
        proposal = await service.create_proposal(
            CreatePendingAppointmentCommand(
                actor=_actor(),
                service_id=1,
                resource_id=1,
                start_at=START,
                customer_phone="+919123456789",
                expires_at=utcnow() + timedelta(minutes=15),
                idempotency_key="tenant-test",
            )
        )

        from fonely.domain.pending_actions.errors import PendingActionNotFoundError

        cross_tenant = _actor(business_id=999)
        with pytest.raises(PendingActionNotFoundError):
            await service.confirm_and_commit(
                ConfirmPendingAppointmentCommand(
                    actor=cross_tenant,
                    pending_action_id=proposal.pending_action_id,
                    expected_version=proposal.version,
                )
            )
        await session.rollback()


async def test_adjacent_slots_succeed(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        r1 = await _create_and_confirm(session, idempotency_key="slot-1", start_at=START)
        assert isinstance(r1, PreCommitAppointmentSuccess)
        await session.commit()

    async with pg_session_factory() as session:
        adjacent_start = END
        r2 = await _create_and_confirm(session, idempotency_key="slot-2", start_at=adjacent_start)
        assert isinstance(r2, PreCommitAppointmentSuccess)
        alloc_count = await session.scalar(select(func.count(ResourceAllocation.id)))
        assert alloc_count == 2
        await session.rollback()


async def test_different_resources_same_time_succeed(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        await session.execute(
            text(
                "INSERT INTO resources "
                "(id, business_id, name, resource_type, is_active) "
                "VALUES (2, 1, 'Mira', 'staff', true)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO service_resource_eligibility "
                "(business_id, service_id, resource_id, is_active) "
                "VALUES (1, 1, 2, true)"
            )
        )
        await session.flush()

        r1 = await _create_and_confirm(session, idempotency_key="res-1-slot")
        assert isinstance(r1, PreCommitAppointmentSuccess)
        await session.commit()

    async with pg_session_factory() as session:
        facts2 = _facts(resource_id=2)
        r2 = await _create_and_confirm(
            session,
            validation=StubValidationPort(facts=facts2),
            idempotency_key="res-2-slot",
        )
        assert isinstance(r2, PreCommitAppointmentSuccess)
        await session.rollback()


async def test_two_confirmations_in_one_outer_transaction(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        await session.execute(
            text(
                "INSERT INTO resources "
                "(id, business_id, name, resource_type, is_active) "
                "VALUES (2, 1, 'Mira', 'staff', true)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO service_resource_eligibility "
                "(business_id, service_id, resource_id, is_active) "
                "VALUES (1, 1, 2, true)"
            )
        )
        await session.flush()

        r1 = await _create_and_confirm(session, idempotency_key="dual-1", start_at=START)
        assert isinstance(r1, PreCommitAppointmentSuccess)

        shifted = START + timedelta(hours=3)
        r2 = await _create_and_confirm(
            session,
            validation=StubValidationPort(facts=_facts(start_at=shifted, resource_id=2)),
            idempotency_key="dual-2",
            start_at=shifted,
        )
        assert isinstance(r2, PreCommitAppointmentSuccess)

        appt_count = await session.scalar(select(func.count(Appointment.id)))
        alloc_count = await session.scalar(select(func.count(ResourceAllocation.id)))
        assert appt_count == 2
        assert alloc_count == 2
        await session.rollback()


async def test_overlapping_confirmation_returns_retryable_failure(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)

        winner = await _create_and_confirm(session, idempotency_key="winner-slot", start_at=START)
        assert isinstance(winner, PreCommitAppointmentSuccess)
        await session.commit()

    async with pg_session_factory() as session:
        overlap_start = START + timedelta(minutes=15)
        loser = await _create_and_confirm(
            session,
            idempotency_key="loser-slot",
            start_at=overlap_start,
        )
        assert isinstance(loser, PreCommitAppointmentFailure)
        assert loser.error_code == "resource_unavailable"
        assert loser.retryable is True

        appt_count = await session.scalar(
            select(func.count(Appointment.id)).where(
                Appointment.idempotency_key == "pa-" + str(loser.pending_action_id)
            )
        )
        assert appt_count == 0

        alloc_count = await session.scalar(
            select(func.count(ResourceAllocation.id)).where(
                ResourceAllocation.pending_action_id == loser.pending_action_id
            )
        )
        assert alloc_count == 0

        pa = (
            await session.execute(
                select(PendingAction).where(PendingAction.id == loser.pending_action_id)
            )
        ).scalar_one()
        assert pa.status == "awaiting_confirmation"
        assert pa.commit_error_code == "resource_unavailable"

        await session.execute(text("SELECT 1"))
        await session.rollback()
