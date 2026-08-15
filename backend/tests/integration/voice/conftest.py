"""Shared seed fixture for the voice integration tests.

test_call_provenance / test_idempotency_replay / test_real_booking exercise the
real booking path against ``fonely.core.database.async_session`` (i.e. the
``DATABASE_URL`` database). They need a clinic to already exist — business 1 with
a "Scaling" service, a "Dr. Priya" resource (id 1), their eligibility, and wide
operating hours — but historically that state came from an AMBIENT, pre-populated
dev database, so the tests were not reproducible on a fresh CI database (they
failed at ``unknown_service:scaling`` before reaching the code under test).

This fixture makes the precondition explicit and deterministic. Tests REQUEST it
by name (no autouse, no import-time side effects): a fresh migrated database plus
``voice_clinic_seed`` is a fully reproducible environment. Seeding runs in the
same ``async_session`` binding the tests use, and is idempotent so re-runs and
already-seeded databases both work.

Scope: test infrastructure only. No product code and no migration change.
"""

from __future__ import annotations

from datetime import time as dt_time

import pytest_asyncio
from sqlalchemy import text

# The values the voice tests assert against are load-bearing:
#   * resolve_service() matches the phrase "scaling" to a service whose name
#     contains "scaling" (case-insensitive substring) — hence name "Scaling".
#   * the tests pass resource_id=1 and business_id=1 literally, so those ids are
#     fixed, not arbitrary.
_BUSINESS_ID = 1
_SERVICE_ID = 1
_RESOURCE_ID = 1
_SERVICE_NAME = "Scaling"
_RESOURCE_NAME = "Dr. Priya"
_OWNER_PHONE = "+919000000001"
_WHATSAPP_PNID = "voice-seed-pnid-1"
# Wide hours so a booking at 17:15 / 18:30-19:00 lands inside opening time.
_OPEN_TIME = dt_time(9, 0)
_CLOSE_TIME = dt_time(19, 30)


async def _seed_voice_clinic(session) -> None:
    """Idempotently seed the reproducible voice-test clinic (business 1)."""
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:bid, 'Voice Test Clinic', 'dental', :phone, 'Asia/Kolkata', 'trial') "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"bid": _BUSINESS_ID, "phone": _OWNER_PHONE},
    )
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (:bid, :phone, 'owner', true) "
            "ON CONFLICT DO NOTHING"
        ),
        {"bid": _BUSINESS_ID, "phone": _OWNER_PHONE},
    )
    await session.execute(
        text(
            "INSERT INTO business_whatsapp_channels "
            "(business_id, phone_number_id, status, is_primary) "
            "VALUES (:bid, :pnid, 'active', true) "
            "ON CONFLICT (phone_number_id) DO NOTHING"
        ),
        {"bid": _BUSINESS_ID, "pnid": _WHATSAPP_PNID},
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (:sid, :bid, :name, 30, 0, 0, 500.00, true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"sid": _SERVICE_ID, "bid": _BUSINESS_ID, "name": _SERVICE_NAME},
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (:rid, :bid, :name, 'staff', true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"rid": _RESOURCE_ID, "bid": _BUSINESS_ID, "name": _RESOURCE_NAME},
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) "
            "VALUES (:bid, :sid, :rid, true) "
            "ON CONFLICT DO NOTHING"
        ),
        {"bid": _BUSINESS_ID, "sid": _SERVICE_ID, "rid": _RESOURCE_ID},
    )
    # Open every day of the week so a booking on any target date is in-hours.
    for dow in range(0, 7):
        await session.execute(
            text(
                "INSERT INTO operating_schedules "
                "(business_id, day_of_week, open_time, close_time, is_active) "
                "VALUES (:bid, :dow, :open, :close, true) "
                "ON CONFLICT DO NOTHING"
            ),
            {"bid": _BUSINESS_ID, "dow": dow, "open": _OPEN_TIME, "close": _CLOSE_TIME},
        )
    await session.commit()


@pytest_asyncio.fixture
async def voice_clinic_seed():
    """Explicit precondition: a fully-seeded reproducible clinic for voice tests.

    Request this fixture from any voice integration test that books against
    business 1. Seeds into the same async_session the tests use; idempotent.
    """
    from fonely.core.database import async_session

    async with async_session() as session:
        await _seed_voice_clinic(session)
    yield
