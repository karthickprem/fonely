"""PostgreSQL evidence for authoritative scheduling policy and capacity."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.appointments.availability import schedule_weekday
from fonely.services.availability import AvailabilityReason, AvailabilityService

pytestmark = pytest.mark.postgres

KOLKATA = ZoneInfo("Asia/Kolkata")


def _target(days: int = 2) -> date:
    return datetime.now(KOLKATA).date() + timedelta(days=days)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=KOLKATA)


async def _seed_clinic(
    session: AsyncSession,
    *,
    business_id: int = 1,
    resource_id: int = 1,
    service_id: int = 1,
    horizon: int = 90,
    notice: int = 0,
    interval: int = 15,
    buffer_before: int = 0,
    buffer_after: int = 0,
) -> None:
    phone = f"+91442835{business_id:04d}"
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription, "
            "appointment_booking_horizon_days, appointment_minimum_notice_minutes, "
            "appointment_slot_interval_minutes) VALUES "
            "(:bid, :name, 'clinic', :phone, 'Asia/Kolkata', 'trial', :horizon, :notice, :interval)"
        ),
        {
            "bid": business_id,
            "name": f"Clinic {business_id}",
            "phone": phone,
            "horizon": horizon,
            "notice": notice,
            "interval": interval,
        },
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (:rid, :bid, :name, 'staff', true)"
        ),
        {"rid": resource_id, "bid": business_id, "name": f"Doctor {resource_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, is_active) VALUES "
            "(:sid, :bid, 'Consultation', 30, :before, :after, true)"
        ),
        {
            "sid": service_id,
            "bid": business_id,
            "before": buffer_before,
            "after": buffer_after,
        },
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) "
            "VALUES (:bid, :sid, :rid, true)"
        ),
        {"bid": business_id, "sid": service_id, "rid": resource_id},
    )
    await session.flush()


async def _add_schedule(
    session: AsyncSession,
    day: date,
    open_at: time,
    close_at: time,
    *,
    business_id: int = 1,
    resource_id: int | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, resource_id, day_of_week, open_time, close_time, is_active) "
            "VALUES (:bid, :rid, :dow, :open, :close, true)"
        ),
        {
            "bid": business_id,
            "rid": resource_id,
            "dow": schedule_weekday(day),
            "open": open_at,
            "close": close_at,
        },
    )
    await session.flush()


async def _add_exception(
    session: AsyncSession,
    day: date,
    *,
    is_closed: bool,
    open_at: time | None = None,
    close_at: time | None = None,
    business_id: int = 1,
    resource_id: int | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO schedule_exceptions "
            "(business_id, resource_id, exception_date, is_closed, open_time, close_time, reason) "
            "VALUES (:bid, :rid, :day, :closed, :open, :close, 'Test exception')"
        ),
        {
            "bid": business_id,
            "rid": resource_id,
            "day": day,
            "closed": is_closed,
            "open": open_at,
            "close": close_at,
        },
    )
    await session.flush()


async def _add_owner_block(
    session: AsyncSession,
    start_at: datetime,
    end_at: datetime,
    *,
    business_id: int = 1,
    resource_id: int = 1,
    key: str = "block-1",
    status: str = "active",
) -> int:
    result = await session.execute(
        text(
            "INSERT INTO resource_allocations "
            "(business_id, resource_id, allocation_type, status, source, "
            "effective_start_at, effective_end_at, reason, idempotency_key, version) "
            "VALUES (:bid, :rid, 'owner_block', :status, 'owner_block', "
            ":start, :end, 'Owner block', :key, 1) RETURNING id"
        ),
        {
            "bid": business_id,
            "rid": resource_id,
            "status": status,
            "start": start_at.astimezone(UTC),
            "end": end_at.astimezone(UTC),
            "key": key,
        },
    )
    await session.flush()
    return result.scalar_one()


async def _add_manual_appointment(
    session: AsyncSession,
    start_at: datetime,
    *,
    appointment_id: int = 1,
    business_id: int = 1,
    resource_id: int = 1,
    service_id: int = 1,
) -> None:
    end_at = start_at + timedelta(minutes=30)
    await session.execute(
        text(
            "INSERT INTO appointments "
            "(id, business_id, resource_id, service_id, customer_name, customer_phone, "
            "start_at, end_at, effective_start_at, effective_end_at, service_name_snapshot, "
            "resource_name_snapshot, duration_minutes_snapshot, buffer_before_minutes_snapshot, "
            "buffer_after_minutes_snapshot, business_timezone_snapshot, status, source, "
            "idempotency_key, version) VALUES "
            "(:aid, :bid, :rid, :sid, 'Patient', '+919000000001', :start, :end, :start, :end, "
            "'Consultation', 'Doctor', 30, 0, 0, 'Asia/Kolkata', 'confirmed', "
            "'owner_manual', :appointment_key, 1)"
        ),
        {
            "aid": appointment_id,
            "bid": business_id,
            "rid": resource_id,
            "sid": service_id,
            "start": start_at.astimezone(UTC),
            "end": end_at.astimezone(UTC),
            "appointment_key": f"manual-{business_id}-{appointment_id}",
        },
    )
    await session.execute(
        text(
            "INSERT INTO resource_allocations "
            "(business_id, resource_id, appointment_id, allocation_type, status, source, "
            "effective_start_at, effective_end_at, idempotency_key, version) VALUES "
            "(:bid, :rid, :aid, 'manual_appointment', 'active', 'owner_manual', "
            ":start, :end, :allocation_key, 1)"
        ),
        {
            "bid": business_id,
            "rid": resource_id,
            "aid": appointment_id,
            "start": start_at.astimezone(UTC),
            "end": end_at.astimezone(UTC),
            "allocation_key": f"manual-allocation-{business_id}-{appointment_id}",
        },
    )
    await session.flush()


async def test_close_early_and_full_closure(pg_session: AsyncSession) -> None:
    day = _target()
    await _seed_clinic(pg_session)
    await _add_schedule(pg_session, day, time(10), time(20))
    await _add_exception(pg_session, day, is_closed=False, open_at=time(10), close_at=time(17))
    service = AvailabilityService(pg_session)

    slots = await service.get_available_slots(1, 1, 1, day, now=_at(day, 8))
    assert slots
    assert max(slot.end_at.astimezone(KOLKATA).time() for slot in slots) <= time(17)
    decision = await service.check_exact_slot(1, 1, 1, _at(day, 18), now=_at(day, 8))
    assert decision.reason == AvailabilityReason.OUTSIDE_OPERATING_HOURS

    await pg_session.rollback()
    await _seed_clinic(pg_session)
    await _add_schedule(pg_session, day, time(10), time(20))
    await _add_exception(pg_session, day, is_closed=True)
    assert (
        await AvailabilityService(pg_session).get_available_slots(1, 1, 1, day, now=_at(day, 8))
        == []
    )


async def test_resource_hours_cannot_widen_business_hours(pg_session: AsyncSession) -> None:
    day = _target()
    await _seed_clinic(pg_session)
    await _add_schedule(pg_session, day, time(9), time(18))
    await _add_schedule(pg_session, day, time(8), time(20), resource_id=1)

    slots = await AvailabilityService(pg_session).get_available_slots(1, 1, 1, day, now=_at(day, 7))
    assert slots[0].start_at.astimezone(KOLKATA).time() == time(9)
    assert slots[-1].end_at.astimezone(KOLKATA).time() == time(18)


async def test_local_midnight_uses_local_weekday_and_exception(pg_session: AsyncSession) -> None:
    day = _target()
    await _seed_clinic(pg_session)
    await _add_schedule(pg_session, day, time(0), time(2))
    requested = _at(day, 0, 30)
    assert requested.astimezone(UTC).date() == day - timedelta(days=1)

    decision = await AvailabilityService(pg_session).check_exact_slot(
        1, 1, 1, requested, now=_at(day - timedelta(days=1), 20)
    )
    assert decision.available

    await _add_exception(pg_session, day, is_closed=True)
    closed = await AvailabilityService(pg_session).check_exact_slot(
        1, 1, 1, requested, now=_at(day - timedelta(days=1), 20)
    )
    assert closed.reason == AvailabilityReason.NO_OPERATING_HOURS


async def test_exact_slot_and_configured_grid(pg_session: AsyncSession) -> None:
    day = _target()
    await _seed_clinic(pg_session, interval=15)
    await _add_schedule(pg_session, day, time(10), time(13))
    service = AvailabilityService(pg_session)

    exact = await service.check_exact_slot(1, 1, 1, _at(day, 10, 15), now=_at(day, 8))
    assert exact.available
    off_grid = await service.check_exact_slot(1, 1, 1, _at(day, 10, 7), now=_at(day, 8))
    assert off_grid.reason == AvailabilityReason.OFF_GRID
    assert _at(day, 10, 15) in [slot.start_at for slot in off_grid.alternatives]

    await pg_session.rollback()
    await _seed_clinic(pg_session, interval=30)
    await _add_schedule(pg_session, day, time(10), time(13))
    slots = await AvailabilityService(pg_session).get_available_slots(1, 1, 1, day, now=_at(day, 8))
    assert {slot.start_at.astimezone(KOLKATA).minute for slot in slots} == {0, 30}
    rejected = await AvailabilityService(pg_session).check_exact_slot(
        1, 1, 1, _at(day, 10, 15), now=_at(day, 8)
    )
    assert rejected.reason == AvailabilityReason.OFF_GRID


async def test_minimum_notice_boundaries(pg_session: AsyncSession) -> None:
    day = _target()
    await _seed_clinic(pg_session, notice=60)
    await _add_schedule(pg_session, day, time(9), time(13))
    now = _at(day, 9)
    service = AvailabilityService(pg_session)

    assert (await service.check_exact_slot(1, 1, 1, _at(day, 10), now=now)).available
    inside = await service.check_exact_slot(1, 1, 1, _at(day, 9, 45), now=now)
    assert inside.reason == AvailabilityReason.INSUFFICIENT_NOTICE


async def test_booking_horizon_local_date_boundaries(pg_session: AsyncSession) -> None:
    today = _target()
    final_day = today + timedelta(days=2)
    beyond = final_day + timedelta(days=1)
    await _seed_clinic(pg_session, horizon=2)
    await _add_schedule(pg_session, final_day, time(9), time(13))
    await _add_schedule(pg_session, beyond, time(9), time(13))
    now = _at(today, 8)
    service = AvailabilityService(pg_session)

    assert (await service.check_exact_slot(1, 1, 1, _at(final_day, 10), now=now)).available
    rejected = await service.check_exact_slot(1, 1, 1, _at(beyond, 10), now=now)
    assert rejected.reason == AvailabilityReason.OUTSIDE_BOOKING_HORIZON


async def test_owner_block_and_half_open_adjacency(pg_session: AsyncSession) -> None:
    day = _target()
    await _seed_clinic(pg_session)
    await _add_schedule(pg_session, day, time(10), time(13))
    await _add_owner_block(pg_session, _at(day, 11), _at(day, 11, 30))
    service = AvailabilityService(pg_session)

    assert not (await service.check_exact_slot(1, 1, 1, _at(day, 11), now=_at(day, 8))).available
    assert (await service.check_exact_slot(1, 1, 1, _at(day, 10, 30), now=_at(day, 8))).available
    assert (await service.check_exact_slot(1, 1, 1, _at(day, 11, 30), now=_at(day, 8))).available


async def test_cross_midnight_allocation_is_loaded_by_overlap(pg_session: AsyncSession) -> None:
    day = _target()
    await _seed_clinic(pg_session)
    await _add_schedule(pg_session, day, time(0), time(3))
    await _add_owner_block(
        pg_session,
        _at(day - timedelta(days=1), 23, 45),
        _at(day, 0, 45),
    )

    decision = await AvailabilityService(pg_session).check_exact_slot(
        1, 1, 1, _at(day, 0, 15), now=_at(day - timedelta(days=1), 20)
    )
    assert decision.reason == AvailabilityReason.CAPACITY_CONFLICT


async def test_other_tenant_and_resource_allocations_do_not_interfere(
    pg_session: AsyncSession,
) -> None:
    day = _target()
    await _seed_clinic(pg_session, business_id=1, resource_id=1, service_id=1)
    await _seed_clinic(pg_session, business_id=2, resource_id=2, service_id=2)
    await _add_schedule(pg_session, day, time(10), time(13), business_id=1)
    await _add_schedule(pg_session, day, time(10), time(13), business_id=2)
    await _add_owner_block(pg_session, _at(day, 11), _at(day, 11, 30), business_id=2, resource_id=2)

    decision = await AvailabilityService(pg_session).check_exact_slot(
        1, 1, 1, _at(day, 11), now=_at(day, 8)
    )
    assert decision.available


async def test_reschedule_excludes_only_original_allocation(pg_session: AsyncSession) -> None:
    day = _target()
    await _seed_clinic(pg_session)
    await _add_schedule(pg_session, day, time(10), time(13))
    await _add_manual_appointment(pg_session, _at(day, 11), appointment_id=1)
    service = AvailabilityService(pg_session)

    without_exclusion = await service.check_exact_slot(1, 1, 1, _at(day, 11), now=_at(day, 8))
    assert without_exclusion.reason == AvailabilityReason.CAPACITY_CONFLICT
    same_time = await service.check_exact_slot(
        1, 1, 1, _at(day, 11), now=_at(day, 8), exclude_appointment_id=1
    )
    assert same_time.available

    await _add_owner_block(pg_session, _at(day, 11, 30), _at(day, 12), key="other-patient")
    unrelated = await service.check_exact_slot(
        1, 1, 1, _at(day, 11, 30), now=_at(day, 8), exclude_appointment_id=1
    )
    assert unrelated.reason == AvailabilityReason.CAPACITY_CONFLICT


async def test_service_buffers_apply_to_hours_and_capacity(pg_session: AsyncSession) -> None:
    day = _target()
    await _seed_clinic(pg_session, buffer_before=15, buffer_after=15)
    await _add_schedule(pg_session, day, time(10), time(13))
    service = AvailabilityService(pg_session)

    outside = await service.check_exact_slot(1, 1, 1, _at(day, 10), now=_at(day, 8))
    assert outside.reason in {
        AvailabilityReason.OFF_GRID,
        AvailabilityReason.OUTSIDE_OPERATING_HOURS,
    }
    assert (await service.check_exact_slot(1, 1, 1, _at(day, 10, 15), now=_at(day, 8))).available

    await _add_owner_block(pg_session, _at(day, 11), _at(day, 11, 15))
    buffer_conflict = await service.check_exact_slot(1, 1, 1, _at(day, 10, 30), now=_at(day, 8))
    assert buffer_conflict.reason == AvailabilityReason.CAPACITY_CONFLICT
