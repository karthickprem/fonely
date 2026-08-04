"""PostgreSQL integration evidence for appointment cancellation and rescheduling.

Every test exercises real PostgreSQL transactions through the AppointmentService.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.api.internal.validation import InternalValidationPort
from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCancellationCommand,
    ConfirmPendingAppointmentCommand,
    ConfirmPendingAppointmentRescheduleCommand,
    CreatePendingAppointmentCancellationCommand,
    CreatePendingAppointmentCommand,
    CreatePendingAppointmentRescheduleCommand,
)
from fonely.domain.appointments.errors import AppointmentDomainError, AppointmentErrorCode
from fonely.domain.appointments.results import (
    AppointmentCancellationResult,
    AppointmentRescheduleResult,
    PreCommitAppointmentSuccess,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.services.appointments import AppointmentService

pytestmark = pytest.mark.postgres


def _future_start(*, days: int = 2, hour: int = 10, minute: int = 0) -> datetime:
    zone = ZoneInfo("Asia/Kolkata")
    day = datetime.now(zone).date() + timedelta(days=days)
    return datetime.combine(day, time(hour, minute), tzinfo=zone).astimezone(UTC)


def _customer(business_id: int = 1) -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
    )


def _owner(business_id: int = 1) -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone="+919000000001",
        verified_role=CallerRole.OWNER,
    )


async def _seed_dental_clinic(session: AsyncSession, business_id: int = 1) -> None:
    phone = f"+91900000000{business_id}"
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:id, :name, 'dental', :phone, 'Asia/Kolkata', 'trial') "
            "ON CONFLICT DO NOTHING"
        ),
        {"id": business_id, "name": f"Clinic {business_id}", "phone": phone},
    )
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (:bid, :phone, 'owner', true) ON CONFLICT DO NOTHING"
        ),
        {"bid": business_id, "phone": phone},
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, "
            "buffer_before_minutes, buffer_after_minutes, price, is_active) "
            "VALUES (:id, :bid, 'Consultation', 30, 0, 0, 300.00, true) "
            "ON CONFLICT DO NOTHING"
        ),
        {"id": business_id, "bid": business_id},
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (:id, :bid, 'Dr. Priya', 'staff', true) ON CONFLICT DO NOTHING"
        ),
        {"id": business_id, "bid": business_id},
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) "
            "VALUES (:bid, :sid, :rid, true) ON CONFLICT DO NOTHING"
        ),
        {"bid": business_id, "sid": business_id, "rid": business_id},
    )
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT :bid, day, '00:00', '23:59', true FROM generate_series(0, 6) AS day "
            "ON CONFLICT DO NOTHING"
        ),
        {"bid": business_id},
    )
    await session.flush()


async def _create_confirmed_appointment(
    session: AsyncSession,
    *,
    business_id: int = 1,
    start_at: datetime | None = None,
    key_suffix: str = "1",
) -> PreCommitAppointmentSuccess:
    if start_at is None:
        start_at = _future_start()
    now = datetime.now(UTC)
    validation = InternalValidationPort(session)
    service = AppointmentService(session, validation=validation)

    proposal = await service.create_proposal(
        CreatePendingAppointmentCommand(
            actor=_customer(business_id),
            service_id=business_id,
            resource_id=business_id,
            start_at=start_at,
            customer_phone="+919123456789",
            expires_at=now + timedelta(minutes=30),
            idempotency_key=f"create-{key_suffix}",
        )
    )

    result = await service.confirm_and_commit(
        ConfirmPendingAppointmentCommand(
            actor=_customer(business_id),
            pending_action_id=proposal.pending_action_id,
            expected_version=proposal.version,
        )
    )
    assert isinstance(result, PreCommitAppointmentSuccess)
    await session.flush()
    return result


# --- Test A: Full cancellation lifecycle ---


async def test_full_cancellation_lifecycle(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)
        confirmed = await _create_confirmed_appointment(session)
        appt_id = confirmed.appointment.appointment_id

        row = (
            await session.execute(
                text("SELECT status FROM appointments WHERE id = :id"),
                {"id": appt_id},
            )
        ).one()
        assert row[0] == "confirmed"

        alloc = (
            await session.execute(
                text(
                    "SELECT status FROM resource_allocations "
                    "WHERE appointment_id = :id AND status = 'active'"
                ),
                {"id": appt_id},
            )
        ).one()
        assert alloc[0] == "active"

        validation = InternalValidationPort(session)
        service = AppointmentService(session, validation=validation)

        cancel_proposal = await service.create_cancellation_proposal(
            CreatePendingAppointmentCancellationCommand(
                actor=_customer(),
                appointment_id=appt_id,
                expected_appointment_version=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                idempotency_key="cancel-lifecycle",
            )
        )
        assert cancel_proposal.confirmation_facts.operation == "cancel"

        cancel_result = await service.confirm_cancellation(
            ConfirmPendingAppointmentCancellationCommand(
                actor=_customer(),
                pending_action_id=cancel_proposal.pending_action_id,
                expected_version=cancel_proposal.version,
            )
        )
        assert isinstance(cancel_result, AppointmentCancellationResult)
        assert cancel_result.appointment_id == appt_id
        assert cancel_result.cancelled_at is not None

        await session.flush()
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

        appt_after = (
            await session.execute(
                text("SELECT status, cancelled_at FROM appointments WHERE id = :id"),
                {"id": appt_id},
            )
        ).one()
        assert appt_after[0] == "cancelled"
        assert appt_after[1] is not None

        alloc_after = (
            await session.execute(
                text("SELECT status FROM resource_allocations WHERE appointment_id = :id"),
                {"id": appt_id},
            )
        ).one()
        assert alloc_after[0] == "cancelled"

        commit_evidence = (
            await session.execute(
                text(
                    "SELECT operation, reason_code FROM appointment_commits "
                    "WHERE appointment_id = :id"
                ),
                {"id": appt_id},
            )
        ).one()
        assert commit_evidence[0] == "cancel"

        appt_exists = await session.scalar(
            text("SELECT count(*) FROM appointments WHERE id = :id"),
            {"id": appt_id},
        )
        assert appt_exists == 1

        await session.rollback()


# --- Test B: Cancel already-cancelled ---


async def test_cancel_already_cancelled_appointment(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)
        confirmed = await _create_confirmed_appointment(session, key_suffix="cancel2")
        appt_id = confirmed.appointment.appointment_id

        validation = InternalValidationPort(session)
        service = AppointmentService(session, validation=validation)

        proposal = await service.create_cancellation_proposal(
            CreatePendingAppointmentCancellationCommand(
                actor=_customer(),
                appointment_id=appt_id,
                expected_appointment_version=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                idempotency_key="cancel-first",
            )
        )
        await service.confirm_cancellation(
            ConfirmPendingAppointmentCancellationCommand(
                actor=_customer(),
                pending_action_id=proposal.pending_action_id,
                expected_version=proposal.version,
            )
        )
        await session.flush()

        with pytest.raises(AppointmentDomainError) as exc_info:
            await service.create_cancellation_proposal(
                CreatePendingAppointmentCancellationCommand(
                    actor=_customer(),
                    appointment_id=appt_id,
                    expected_appointment_version=2,
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    idempotency_key="cancel-second",
                )
            )
        assert exc_info.value.code == AppointmentErrorCode.INVALID_STATE

        status = await session.scalar(
            text("SELECT status FROM appointments WHERE id = :id"),
            {"id": appt_id},
        )
        assert status == "cancelled"
        await session.rollback()


# --- Test D: Cancellation tenant isolation ---


async def test_cancellation_tenant_isolation(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session, business_id=1)
        await _seed_dental_clinic(session, business_id=2)

        confirmed_b1 = await _create_confirmed_appointment(
            session, business_id=1, key_suffix="tenant-b1"
        )
        appt_id_b1 = confirmed_b1.appointment.appointment_id

        validation = InternalValidationPort(session)
        service = AppointmentService(session, validation=validation)

        with pytest.raises(AppointmentDomainError) as exc_info:
            await service.create_cancellation_proposal(
                CreatePendingAppointmentCancellationCommand(
                    actor=_customer(business_id=2),
                    appointment_id=appt_id_b1,
                    expected_appointment_version=1,
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    idempotency_key="cross-tenant-cancel",
                )
            )
        assert exc_info.value.code == AppointmentErrorCode.NOT_FOUND

        status = await session.scalar(
            text("SELECT status FROM appointments WHERE id = :id"),
            {"id": appt_id_b1},
        )
        assert status == "confirmed"
        await session.rollback()


# --- Test E: Cancellation idempotency ---


async def test_cancellation_proposal_idempotency(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)
        fixed_start = _future_start(hour=11)
        confirmed = await _create_confirmed_appointment(
            session, start_at=fixed_start, key_suffix="idemp"
        )
        appt_id = confirmed.appointment.appointment_id

        validation = InternalValidationPort(session)
        service = AppointmentService(session, validation=validation)

        expires = datetime.now(UTC) + timedelta(minutes=20)
        proposal1 = await service.create_cancellation_proposal(
            CreatePendingAppointmentCancellationCommand(
                actor=_customer(),
                appointment_id=appt_id,
                expected_appointment_version=1,
                expires_at=expires,
                idempotency_key="cancel-idemp",
            )
        )
        proposal2 = await service.create_cancellation_proposal(
            CreatePendingAppointmentCancellationCommand(
                actor=_customer(),
                appointment_id=appt_id,
                expected_appointment_version=1,
                expires_at=expires,
                idempotency_key="cancel-idemp",
            )
        )
        assert proposal1.pending_action_id == proposal2.pending_action_id
        await session.rollback()


# --- Rescheduling PostgreSQL tests ---


async def test_full_rescheduling_lifecycle(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)
        old_start = _future_start(hour=10)
        confirmed = await _create_confirmed_appointment(
            session, start_at=old_start, key_suffix="resched-life"
        )
        appt_id = confirmed.appointment.appointment_id

        validation = InternalValidationPort(session)
        service = AppointmentService(session, validation=validation)

        new_start = _future_start(hour=12)
        proposal = await service.create_reschedule_proposal(
            CreatePendingAppointmentRescheduleCommand(
                actor=_customer(),
                appointment_id=appt_id,
                expected_appointment_version=1,
                service_id=1,
                start_at=new_start,
                expires_at=datetime.now(UTC) + timedelta(minutes=20),
                idempotency_key="resched-lifecycle",
            )
        )
        assert proposal.confirmation_facts.operation == "reschedule"
        assert proposal.confirmation_facts.target_appointment_id == appt_id

        result = await service.confirm_reschedule(
            ConfirmPendingAppointmentRescheduleCommand(
                actor=_customer(),
                pending_action_id=proposal.pending_action_id,
                expected_version=proposal.version,
            )
        )
        assert isinstance(result, AppointmentRescheduleResult)
        assert result.appointment_id == appt_id

        await session.flush()
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

        appt_after = (
            await session.execute(
                text("SELECT status, rescheduled_at, version FROM appointments WHERE id = :id"),
                {"id": appt_id},
            )
        ).one()
        assert appt_after[0] == "confirmed"
        assert appt_after[1] is not None
        assert appt_after[2] == 2

        allocs = (
            await session.execute(
                text(
                    "SELECT status FROM resource_allocations WHERE appointment_id = :id ORDER BY id"
                ),
                {"id": appt_id},
            )
        ).all()
        statuses = [r[0] for r in allocs]
        assert "released" in statuses
        assert "active" in statuses

        commit = (
            await session.execute(
                text("SELECT operation FROM appointment_commits WHERE appointment_id = :id"),
                {"id": appt_id},
            )
        ).one()
        assert commit[0] == "reschedule"

        await session.rollback()


async def test_reschedule_to_conflicting_time(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)
        start1 = _future_start(hour=10)
        start2 = start1 + timedelta(minutes=30)

        confirmed1 = await _create_confirmed_appointment(
            session, start_at=start1, key_suffix="conflict-1"
        )
        await _create_confirmed_appointment(session, start_at=start2, key_suffix="conflict-2")
        appt_id_1 = confirmed1.appointment.appointment_id

        validation = InternalValidationPort(session)
        service = AppointmentService(session, validation=validation)

        with pytest.raises(ValueError, match="capacity_conflict"):
            await service.create_reschedule_proposal(
                CreatePendingAppointmentRescheduleCommand(
                    actor=_customer(),
                    appointment_id=appt_id_1,
                    expected_appointment_version=1,
                    service_id=1,
                    start_at=start2,
                    expires_at=datetime.now(UTC) + timedelta(minutes=20),
                    idempotency_key="resched-conflict",
                )
            )

        appt_unchanged = (
            await session.execute(
                text("SELECT status, version FROM appointments WHERE id = :id"),
                {"id": appt_id_1},
            )
        ).one()
        assert appt_unchanged[0] == "confirmed"
        assert appt_unchanged[1] == 1

        active_alloc = await session.scalar(
            text(
                "SELECT count(*) FROM resource_allocations "
                "WHERE appointment_id = :id AND status = 'active'"
            ),
            {"id": appt_id_1},
        )
        assert active_alloc == 1

        await session.rollback()


async def test_cancellation_replays_from_fresh_session_without_duplicate_evidence(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    shared_expiry = datetime.now(UTC) + timedelta(minutes=20)
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)
        confirmed = await _create_confirmed_appointment(
            session, start_at=_future_start(hour=10), key_suffix="cancel-replay-create"
        )
        appointment_id = confirmed.appointment.appointment_id
        service = AppointmentService(session, validation=InternalValidationPort(session))
        proposal = await service.create_cancellation_proposal(
            CreatePendingAppointmentCancellationCommand(
                actor=_customer(),
                appointment_id=appointment_id,
                expected_appointment_version=1,
                reason_code="customer_request",
                expires_at=shared_expiry,
                idempotency_key="cancel-fresh-replay",
            )
        )
        first = await service.confirm_cancellation(
            ConfirmPendingAppointmentCancellationCommand(
                actor=_customer(),
                pending_action_id=proposal.pending_action_id,
                expected_version=proposal.version,
            )
        )
        await session.commit()

    async with pg_session_factory() as session:
        service = AppointmentService(session, validation=InternalValidationPort(session))
        replayed_proposal = await service.create_cancellation_proposal(
            CreatePendingAppointmentCancellationCommand(
                actor=_customer(),
                appointment_id=appointment_id,
                expected_appointment_version=1,
                reason_code="customer_request",
                expires_at=shared_expiry,
                idempotency_key="cancel-fresh-replay",
            )
        )
        replay = await service.confirm_cancellation(
            ConfirmPendingAppointmentCancellationCommand(
                actor=_customer(),
                pending_action_id=replayed_proposal.pending_action_id,
                expected_version=1,
            )
        )
        assert replay.appointment_commit_id == first.appointment_commit_id
        assert replay.cancelled_at == first.cancelled_at
        assert (
            await session.scalar(
                text("SELECT count(*) FROM appointment_commits WHERE appointment_id = :id"),
                {"id": appointment_id},
            )
            == 1
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM notification_outbox "
                    "WHERE entity_id = :id AND event_type = 'appointment_cancelled'"
                ),
                {"id": appointment_id},
            )
            == 2
        )
        await session.rollback()


async def test_reschedule_replays_from_fresh_session_without_second_mutation(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    new_start = _future_start(hour=12)
    shared_expiry = datetime.now(UTC) + timedelta(minutes=20)
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)
        confirmed = await _create_confirmed_appointment(
            session, start_at=_future_start(hour=10), key_suffix="reschedule-replay-create"
        )
        appointment_id = confirmed.appointment.appointment_id
        service = AppointmentService(session, validation=InternalValidationPort(session))
        proposal = await service.create_reschedule_proposal(
            CreatePendingAppointmentRescheduleCommand(
                actor=_customer(),
                appointment_id=appointment_id,
                expected_appointment_version=1,
                service_id=1,
                start_at=new_start,
                expires_at=shared_expiry,
                idempotency_key="reschedule-fresh-replay",
            )
        )
        first = await service.confirm_reschedule(
            ConfirmPendingAppointmentRescheduleCommand(
                actor=_customer(),
                pending_action_id=proposal.pending_action_id,
                expected_version=proposal.version,
            )
        )
        await session.commit()

    async with pg_session_factory() as session:
        service = AppointmentService(session, validation=InternalValidationPort(session))
        replayed_proposal = await service.create_reschedule_proposal(
            CreatePendingAppointmentRescheduleCommand(
                actor=_customer(),
                appointment_id=appointment_id,
                expected_appointment_version=1,
                service_id=1,
                start_at=new_start,
                expires_at=shared_expiry,
                idempotency_key="reschedule-fresh-replay",
            )
        )
        replay = await service.confirm_reschedule(
            ConfirmPendingAppointmentRescheduleCommand(
                actor=_customer(),
                pending_action_id=replayed_proposal.pending_action_id,
                expected_version=1,
            )
        )
        assert replay.appointment_commit_id == first.appointment_commit_id
        assert replay.version == first.version
        assert replay.start_at == first.start_at
        assert (
            await session.scalar(
                text("SELECT count(*) FROM appointment_commits WHERE appointment_id = :id"),
                {"id": appointment_id},
            )
            == 1
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM resource_allocations WHERE appointment_id = :id"),
                {"id": appointment_id},
            )
            == 2
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM resource_allocations "
                    "WHERE appointment_id = :id AND status = 'active'"
                ),
                {"id": appointment_id},
            )
            == 1
        )
        await session.rollback()


async def test_reschedule_cancelled_appointment(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)
        confirmed = await _create_confirmed_appointment(session, key_suffix="resched-cancelled")
        appt_id = confirmed.appointment.appointment_id

        validation = InternalValidationPort(session)
        service = AppointmentService(session, validation=validation)

        cancel_proposal = await service.create_cancellation_proposal(
            CreatePendingAppointmentCancellationCommand(
                actor=_customer(),
                appointment_id=appt_id,
                expected_appointment_version=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=20),
                idempotency_key="cancel-before-resched",
            )
        )
        await service.confirm_cancellation(
            ConfirmPendingAppointmentCancellationCommand(
                actor=_customer(),
                pending_action_id=cancel_proposal.pending_action_id,
                expected_version=cancel_proposal.version,
            )
        )
        await session.flush()

        with pytest.raises(AppointmentDomainError) as exc_info:
            await service.create_reschedule_proposal(
                CreatePendingAppointmentRescheduleCommand(
                    actor=_customer(),
                    appointment_id=appt_id,
                    expected_appointment_version=2,
                    service_id=1,
                    start_at=_future_start(hour=13),
                    expires_at=datetime.now(UTC) + timedelta(minutes=20),
                    idempotency_key="resched-after-cancel",
                )
            )
        assert exc_info.value.code == AppointmentErrorCode.INVALID_STATE

        status = await session.scalar(
            text("SELECT status FROM appointments WHERE id = :id"),
            {"id": appt_id},
        )
        assert status == "cancelled"
        await session.rollback()
