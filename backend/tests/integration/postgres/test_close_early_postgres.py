"""PostgreSQL integration test for owner close_early command.

Proves that closing a clinic early creates a modified ScheduleException
(not a full closure) and only cancels appointments after the new close time.
"""

from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.validators import utcnow
from fonely.models.schema import Appointment, ScheduleException
from fonely.services.owner_command_parser import ParsedOwnerCommand
from fonely.services.owner_commands import OwnerCommandService

pytestmark = pytest.mark.postgres

IST = timezone(timedelta(hours=5, minutes=30))


async def _seed_clinic(session: AsyncSession, target_date: date) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental', 'dental_clinic', '+910000000001', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Dr. Priya', 'dentist', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, "
            " buffer_before_minutes, buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'Consultation', 30, 0, 0, 500, true)"
        )
    )
    dow = target_date.weekday()
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, resource_id, day_of_week, open_time, close_time, is_active) "
            "VALUES (1, NULL, :dow, '17:00', '20:30', true)"
        ),
        {"dow": dow},
    )
    await session.flush()


async def _insert_appointment(
    session: AsyncSession,
    appt_id: int,
    start_hour: int,
    start_minute: int,
    target_date: date,
) -> int:
    start_ist = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        start_hour,
        start_minute,
        tzinfo=IST,
    )
    end_ist = start_ist + timedelta(minutes=30)

    pa_result = await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(business_id, action_type, payload_schema_version, proposed_payload, "
            " payload_digest, status, committed_entity_type, committed_entity_id, "
            " expires_at, idempotency_key, version) "
            "VALUES (1, 'appointment', 1, '{}'::jsonb, :digest, 'confirmed', "
            " 'appointment', :appt_id, :exp, :key, 1) "
            "RETURNING id"
        ),
        {
            "digest": f"close-early-test-{appt_id}",
            "appt_id": appt_id,
            "exp": start_ist + timedelta(hours=1),
            "key": f"close-early-pa-{appt_id}",
        },
    )
    pa_id = pa_result.scalar_one()

    await session.execute(
        text(
            "INSERT INTO appointments "
            "(id, business_id, resource_id, service_id, customer_phone, customer_name, "
            " start_at, end_at, effective_start_at, effective_end_at, "
            " status, source, service_name_snapshot, resource_name_snapshot, "
            " duration_minutes_snapshot, buffer_before_minutes_snapshot, "
            " buffer_after_minutes_snapshot, business_timezone_snapshot, "
            " pending_action_id, idempotency_key) "
            "VALUES (:id, 1, 1, 1, '+919123456789', 'Patient', "
            " :start, :end, :start, :end, "
            " 'confirmed', 'customer_conversation', 'Consultation', 'Dr. Priya', "
            " 30, 0, 0, 'Asia/Kolkata', :pa_id, :idem)"
        ),
        {
            "id": appt_id,
            "start": start_ist,
            "end": end_ist,
            "pa_id": pa_id,
            "idem": f"pa-close-early-{appt_id}",
        },
    )

    await session.execute(
        text(
            "INSERT INTO resource_allocations "
            "(business_id, resource_id, appointment_id, pending_action_id, "
            " allocation_type, status, source, "
            " effective_start_at, effective_end_at, idempotency_key) "
            "VALUES (1, 1, :appt_id, :pa_id, 'appointment', 'active', "
            " 'customer_conversation', :start, :end, :idem)"
        ),
        {
            "appt_id": appt_id,
            "pa_id": pa_id,
            "start": start_ist,
            "end": end_ist,
            "idem": f"alloc-close-early-{appt_id}",
        },
    )
    await session.flush()
    return pa_id


class TestCloseEarly:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_close_early_cancels_only_after_new_close_time(
        self, pg_session: AsyncSession
    ) -> None:
        tomorrow = (utcnow() + timedelta(days=1)).date()
        await _seed_clinic(pg_session, tomorrow)

        await _insert_appointment(pg_session, 101, 17, 0, tomorrow)
        await _insert_appointment(pg_session, 102, 18, 0, tomorrow)
        await _insert_appointment(pg_session, 103, 19, 0, tomorrow)

        appts_before = (
            await pg_session.execute(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.status == "confirmed")
            )
        ).scalar()
        assert appts_before == 3

        stub_model = None
        svc = OwnerCommandService(pg_session, model=stub_model)  # type: ignore[arg-type]
        parsed = ParsedOwnerCommand(
            command="close_early",
            close_time="6:30 PM",
            date=tomorrow.isoformat(),
        )
        result = await svc._handle_close_early(1, "+910000000001", parsed)

        assert result.success is True
        assert result.command_type == "close_early"
        assert result.affected_appointments == 1

        confirmed = (
            (
                await pg_session.execute(
                    select(Appointment.id)
                    .where(Appointment.status == "confirmed")
                    .order_by(Appointment.id)
                )
            )
            .scalars()
            .all()
        )
        assert 101 in confirmed
        assert 102 in confirmed
        assert 103 not in confirmed

        cancelled = (
            (
                await pg_session.execute(
                    select(Appointment.id).where(Appointment.status == "cancelled")
                )
            )
            .scalars()
            .all()
        )
        assert 103 in cancelled

        exc = await pg_session.scalar(
            select(ScheduleException).where(
                ScheduleException.business_id == 1,
                ScheduleException.exception_date == tomorrow,
            )
        )
        assert exc is not None
        assert exc.is_closed is False
        assert exc.open_time == time(17, 0)
        assert exc.close_time == time(18, 30)
