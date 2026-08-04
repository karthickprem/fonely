"""PostgreSQL integration tests for unified AvailabilityService."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.services.availability import AvailabilityService

pytestmark = pytest.mark.postgres


async def _seed_clinic(session: AsyncSession, *, target_date_iso: str) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental', 'clinic', '+914428350001', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (1, '+914428350001', 'owner', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Dr. Priya Krishnan', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services (id, business_id, name, duration_minutes, is_active) "
            "VALUES (1, 1, 'General Consultation', 30, true)"
        )
    )
    await session.commit()


async def _add_business_schedule(
    session: AsyncSession, day_of_week: int, open_t: time, close_t: time
) -> None:
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, resource_id, day_of_week, open_time, close_time, is_active) "
            "VALUES (1, NULL, :day, :open, :close, true)"
        ),
        {"day": day_of_week, "open": open_t, "close": close_t},
    )
    await session.commit()


async def _add_resource_schedule(
    session: AsyncSession, resource_id: int, day_of_week: int, open_t: time, close_t: time
) -> None:
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, resource_id, day_of_week, open_time, close_time, is_active) "
            "VALUES (1, :rid, :day, :open, :close, true)"
        ),
        {"rid": resource_id, "day": day_of_week, "open": open_t, "close": close_t},
    )
    await session.commit()


async def _add_close_early_exception(
    session: AsyncSession, target_date: date, new_close: time
) -> None:
    await session.execute(
        text(
            "INSERT INTO schedule_exceptions "
            "(business_id, resource_id, exception_date, is_closed, open_time, close_time, reason) "
            "VALUES (1, NULL, :d, false, :open, :close, 'Closing early')"
        ),
        {"d": target_date, "open": time(10, 0), "close": new_close},
    )
    await session.commit()


async def _add_closed_exception(session: AsyncSession, target_date: date) -> None:
    await session.execute(
        text(
            "INSERT INTO schedule_exceptions "
            "(business_id, resource_id, exception_date, is_closed, reason) "
            "VALUES (1, NULL, :d, true, 'Holiday')"
        ),
        {"d": target_date},
    )
    await session.commit()


async def _add_appointment(
    session: AsyncSession,
    resource_id: int,
    start_utc: datetime,
    end_utc: datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "status, expires_at, idempotency_key, version, payload_digest) VALUES "
            "(DEFAULT, 1, 'appointment', 1, :payload, 'confirmed', :exp, "
            ":key, 1, :digest)"
        ),
        {
            "payload": "{}",
            "exp": datetime.now(UTC) + timedelta(hours=24),
            "key": f"pa-{start_utc.isoformat()}",
            "digest": f"d{abs(hash(start_utc.isoformat())):064x}"[:64],
        },
    )
    pa_id = (await session.execute(text("SELECT max(id) FROM pending_actions"))).scalar_one()
    await session.execute(
        text(
            "INSERT INTO appointments "
            "(business_id, resource_id, service_id, customer_name, customer_phone, "
            "start_at, end_at, effective_start_at, effective_end_at, "
            "service_name_snapshot, resource_name_snapshot, "
            "duration_minutes_snapshot, buffer_before_minutes_snapshot, "
            "buffer_after_minutes_snapshot, business_timezone_snapshot, "
            "status, source, idempotency_key, pending_action_id, version) VALUES "
            "(1, :rid, 1, 'Patient', '+919000000001', :start, :end, :start, :end, "
            "'Consultation', 'Dr. Priya', 30, 0, 0, 'Asia/Kolkata', "
            "'confirmed', 'customer_conversation', :key, :pa_id, 1)"
        ),
        {
            "rid": resource_id,
            "start": start_utc,
            "end": end_utc,
            "key": f"appt-{start_utc.isoformat()}",
            "pa_id": pa_id,
        },
    )
    await session.commit()


def _next_weekday(target_isoweekday: int) -> str:
    """Return ISO date string for the next occurrence of the given weekday."""
    today = datetime.now(UTC).date()
    days_ahead = target_isoweekday - today.isoweekday()
    if days_ahead <= 0:
        days_ahead += 7
    return (today + timedelta(days=days_ahead)).isoformat()


async def test_basic_slots_returned(pg_session: AsyncSession) -> None:
    target_date_iso = _next_weekday(1)  # Monday
    await _seed_clinic(pg_session, target_date_iso=target_date_iso)
    await _add_business_schedule(pg_session, 1, time(10, 0), time(13, 0))

    from datetime import date as date_type

    svc = AvailabilityService(pg_session)
    slots = await svc.get_available_slots(
        business_id=1,
        resource_id=1,
        target_date=date_type.fromisoformat(target_date_iso),
        service_duration_minutes=30,
    )
    assert len(slots) > 0
    for slot in slots:
        assert slot.resource_id == 1
        assert slot.resource_name == "Dr. Priya Krishnan"


async def test_close_early_blocks_slots_after_new_close(pg_session: AsyncSession) -> None:
    """P0 bug: owner says 'close at 5 PM' but patient can still book at 6 PM."""
    target_date_iso = _next_weekday(2)  # Tuesday
    await _seed_clinic(pg_session, target_date_iso=target_date_iso)
    await _add_business_schedule(pg_session, 2, time(10, 0), time(20, 0))
    await _add_close_early_exception(pg_session, date.fromisoformat(target_date_iso), time(17, 0))

    from datetime import date as date_type
    from zoneinfo import ZoneInfo

    svc = AvailabilityService(pg_session)
    slots = await svc.get_available_slots(
        business_id=1,
        resource_id=1,
        target_date=date_type.fromisoformat(target_date_iso),
        service_duration_minutes=30,
    )

    tz = ZoneInfo("Asia/Kolkata")
    for slot in slots:
        local_end = slot.end_at.astimezone(tz)
        assert local_end.time() <= time(17, 0), (
            f"Slot ends at {local_end.time()}, but clinic closes early at 17:00"
        )

    assert len(slots) > 0, "Should still have morning/afternoon slots"


async def test_close_early_is_slot_available_rejects(pg_session: AsyncSession) -> None:
    """is_slot_available must reject slots after close_early time."""
    target_date_iso = _next_weekday(3)  # Wednesday
    await _seed_clinic(pg_session, target_date_iso=target_date_iso)
    await _add_business_schedule(pg_session, 3, time(10, 0), time(20, 0))
    await _add_close_early_exception(pg_session, date.fromisoformat(target_date_iso), time(17, 0))

    from datetime import date as date_type
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Kolkata")
    target = date_type.fromisoformat(target_date_iso)

    svc = AvailabilityService(pg_session)

    slot_6pm = datetime.combine(target, time(18, 0), tzinfo=tz)
    available, reason = await svc.is_slot_available(
        business_id=1, resource_id=1, start_at=slot_6pm, duration_minutes=30
    )
    assert not available
    assert "operating hours" in reason.lower()

    slot_11am = datetime.combine(target, time(11, 0), tzinfo=tz)
    available, reason = await svc.is_slot_available(
        business_id=1, resource_id=1, start_at=slot_11am, duration_minutes=30
    )
    assert available


async def test_full_closure_returns_no_slots(pg_session: AsyncSession) -> None:
    target_date_iso = _next_weekday(4)  # Thursday
    await _seed_clinic(pg_session, target_date_iso=target_date_iso)
    await _add_business_schedule(pg_session, 4, time(10, 0), time(20, 0))
    await _add_closed_exception(pg_session, date.fromisoformat(target_date_iso))

    from datetime import date as date_type

    svc = AvailabilityService(pg_session)
    slots = await svc.get_available_slots(
        business_id=1,
        resource_id=1,
        target_date=date_type.fromisoformat(target_date_iso),
        service_duration_minutes=30,
    )
    assert slots == []


async def test_resource_schedule_takes_precedence(pg_session: AsyncSession) -> None:
    """When resource has its own schedule, it should be intersected with business hours."""
    target_date_iso = _next_weekday(5)  # Friday
    await _seed_clinic(pg_session, target_date_iso=target_date_iso)
    await _add_business_schedule(pg_session, 5, time(9, 0), time(18, 0))
    await _add_resource_schedule(pg_session, 1, 5, time(10, 0), time(14, 0))

    from datetime import date as date_type
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Kolkata")
    svc = AvailabilityService(pg_session)
    slots = await svc.get_available_slots(
        business_id=1,
        resource_id=1,
        target_date=date_type.fromisoformat(target_date_iso),
        service_duration_minutes=30,
    )

    for slot in slots:
        local_start = slot.start_at.astimezone(tz)
        local_end = slot.end_at.astimezone(tz)
        assert local_start.time() >= time(10, 0), (
            f"Slot starts at {local_start.time()}, before resource opens at 10:00"
        )
        assert local_end.time() <= time(14, 0), (
            f"Slot ends at {local_end.time()}, after resource closes at 14:00"
        )


async def test_existing_appointment_blocks_slot(pg_session: AsyncSession) -> None:
    target_date_iso = _next_weekday(6)  # Saturday
    await _seed_clinic(pg_session, target_date_iso=target_date_iso)
    await _add_business_schedule(pg_session, 6, time(10, 0), time(13, 0))

    from datetime import date as date_type
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Kolkata")
    target = date_type.fromisoformat(target_date_iso)

    booked_start = datetime.combine(target, time(11, 0), tzinfo=tz).astimezone(UTC)
    booked_end = booked_start + timedelta(minutes=30)
    await _add_appointment(pg_session, 1, booked_start, booked_end)

    svc = AvailabilityService(pg_session)
    slots = await svc.get_available_slots(
        business_id=1,
        resource_id=1,
        target_date=target,
        service_duration_minutes=30,
    )

    for slot in slots:
        local_start = slot.start_at.astimezone(tz)
        assert local_start.time() != time(11, 0), "11:00 slot should be blocked by appointment"


async def test_exact_slot_matching_no_silent_shift(pg_session: AsyncSession) -> None:
    """is_slot_available must not silently shift times — exact match required."""
    target_date_iso = _next_weekday(1)
    await _seed_clinic(pg_session, target_date_iso=target_date_iso)
    await _add_business_schedule(pg_session, 1, time(10, 0), time(13, 0))

    from datetime import date as date_type
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Kolkata")
    target = date_type.fromisoformat(target_date_iso)

    svc = AvailabilityService(pg_session)

    slot_before_open = datetime.combine(target, time(9, 0), tzinfo=tz)
    available, _ = await svc.is_slot_available(
        business_id=1, resource_id=1, start_at=slot_before_open, duration_minutes=30
    )
    assert not available

    slot_past_close = datetime.combine(target, time(12, 45), tzinfo=tz)
    available, _ = await svc.is_slot_available(
        business_id=1, resource_id=1, start_at=slot_past_close, duration_minutes=30
    )
    assert not available
