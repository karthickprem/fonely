"""PostgreSQL contracts for D3 appointment create-and-confirm transaction.

Collected everywhere; executed only against the guarded local PostgreSQL test
database configured by the integration-test fixtures.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from fonely.domain.pending_actions.payloads import (
    AppointmentFacts,
    CreateAppointmentData,
    PendingAppointmentEnvelope,
)
from fonely.models.enums import CallerRole, PendingActionType
from fonely.models.schema import Appointment, PendingAction, ResourceAllocation
from fonely.services.appointments import AppointmentService

pytestmark = pytest.mark.postgres

START = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)


def _actor(business_id: int = 1) -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone="+919123456789",
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
    await session.flush()


async def _create_and_confirm(
    session: AsyncSession,
    *,
    validation: AppointmentValidationPort | None = None,
    idempotency_key: str = "test-appt-1",
    start_at: datetime = START,
) -> PreCommitAppointmentSuccess | PreCommitAppointmentFailure:
    v = validation or StubValidationPort(facts=_facts(start_at=start_at))
    service = AppointmentService(session, validation=v)

    proposal = await service.create_proposal(
        CreatePendingAppointmentCommand(
            actor=_actor(),
            service_id=1,
            start_at=start_at,
            customer_phone="+919123456789",
            expires_at=start_at + timedelta(hours=1),
            idempotency_key=idempotency_key,
        )
    )

    result = await service.confirm_and_commit(
        ConfirmPendingAppointmentCommand(
            actor=_actor(),
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
                start_at=START,
                customer_phone="+919123456789",
                expires_at=START + timedelta(hours=1),
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


async def test_replay_returns_same_appointment(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        result1 = await _create_and_confirm(session)
        assert isinstance(result1, PreCommitAppointmentSuccess)
        await session.commit()

    async with pg_session_factory() as session:
        service = AppointmentService(session, validation=StubValidationPort())
        proposal = await service.create_proposal(
            CreatePendingAppointmentCommand(
                actor=_actor(),
                service_id=1,
                start_at=START,
                customer_phone="+919123456789",
                expires_at=START + timedelta(hours=1),
                idempotency_key="test-appt-1",
            )
        )
        result2 = await service.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=_actor(),
                pending_action_id=proposal.pending_action_id,
                expected_version=proposal.version,
            )
        )
        assert isinstance(result2, PreCommitAppointmentSuccess)
        assert result2.appointment.appointment_id == result1.appointment.appointment_id
        await session.rollback()


async def test_adjacent_slots_succeed(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        r1 = await _create_and_confirm(session, idempotency_key="slot-1", start_at=START)
        assert isinstance(r1, PreCommitAppointmentSuccess)

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
                "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
                "VALUES (2, 1, 'Mira', 'staff', true)"
            )
        )
        await session.flush()

        r1 = await _create_and_confirm(session, idempotency_key="res-1-slot")
        assert isinstance(r1, PreCommitAppointmentSuccess)

        facts2 = _facts(resource_id=2)
        r2 = await _create_and_confirm(
            session,
            validation=StubValidationPort(facts=facts2),
            idempotency_key="res-2-slot",
        )
        assert isinstance(r2, PreCommitAppointmentSuccess)
        await session.rollback()
