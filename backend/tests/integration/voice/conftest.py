"""Shared seed + teardown fixture for the voice integration tests.

test_call_provenance / test_idempotency_replay / test_real_booking exercise the
real booking path against ``fonely.core.database.async_session`` (i.e. the
``DATABASE_URL`` database). They need a clinic to already exist — business 1 with
a "Scaling" service, a "Dr. Priya" resource (id 1), their eligibility, and wide
operating hours — but historically that state came from an AMBIENT, pre-populated
dev database, so the tests were not reproducible on a fresh CI database (they
failed at ``unknown_service:scaling`` before reaching the code under test).

This fixture makes the precondition explicit and deterministic AND cleans up after
itself. It is required explicitly by each voice test (no autouse, no import-time
side effects). Critically, these voice tests live outside ``tests/integration/
postgres/`` and so are NOT covered by that package's ``clean_database`` autouse
truncate — without the teardown here they leave a ``business_whatsapp_channels``
row (and the ``calls``/booking rows the tests create) behind, which then trips the
0016 downgrade guard during the CI ``alembic downgrade base`` step. So:

  * SETUP asserts business 1 does NOT already exist (empty-business precondition —
    this is a dedicated fresh/migrated-DB fixture; an ambient business 1 means we
    do NOT own the data and must refuse rather than risk deleting real rows), then
    inserts the deterministic clinic EXPLICITLY (no ON CONFLICT — ownership is
    unambiguous because the precondition proved the slot empty).
  * TEARDOWN deletes, in FK-safe order derived from the live schema, every row
    under business 1 — both the seed rows AND the calls/appointments/pending
    actions/allocations/notifications the tests create — in its own transaction
    with rollback-on-error, then fails loudly. Guarded tables (0016 whatsapp
    channels, 0018 dpdp calls, 0019/0020 callbacks) end at zero, so a subsequent
    ``alembic downgrade base`` succeeds. No blanket truncate, no clearing of DPDP
    fields, no guard weakening.

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
#     fixed, not arbitrary. That is also why we can't use a dedicated random
#     business id; instead we assert the fixed slot is empty and own it fully.
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

# FK-safe deletion order for everything the voice tests create under business 1.
# Derived from the live foreign-key graph (children strictly before parents):
#   resource_allocations -> appointments
#   appointment_commits   -> appointments
#   appointments          -> calls, pending_actions, businesses
#   {notification_outbox, notification_manifests, pending_actions, calls,
#    business_whatsapp_channels, service_resource_eligibility,
#    operating_schedules, resources, services, business_users} -> businesses
# Child tables are scoped to business 1 via a subquery on the guarded parent.
_TEARDOWN_STATEMENTS = (
    "DELETE FROM resource_allocations WHERE appointment_id IN "
    "(SELECT id FROM appointments WHERE business_id = :bid)",
    "DELETE FROM appointment_commits WHERE business_id = :bid",
    "DELETE FROM appointments WHERE business_id = :bid",
    "DELETE FROM notification_outbox WHERE business_id = :bid",
    "DELETE FROM notification_manifests WHERE business_id = :bid",
    "DELETE FROM pending_actions WHERE business_id = :bid",
    "DELETE FROM calls WHERE business_id = :bid",
    "DELETE FROM business_whatsapp_channels WHERE business_id = :bid",
    "DELETE FROM service_resource_eligibility WHERE business_id = :bid",
    "DELETE FROM operating_schedules WHERE business_id = :bid",
    "DELETE FROM resources WHERE business_id = :bid",
    "DELETE FROM services WHERE business_id = :bid",
    "DELETE FROM business_users WHERE business_id = :bid",
    "DELETE FROM businesses WHERE id = :bid",
)


async def _assert_business_absent(session) -> None:
    exists = await session.scalar(
        text("SELECT 1 FROM businesses WHERE id = :bid"), {"bid": _BUSINESS_ID}
    )
    if exists:
        raise RuntimeError(
            f"voice_clinic_seed precondition failed: business {_BUSINESS_ID} already "
            "exists. This fixture owns and deletes all business-"
            f"{_BUSINESS_ID} rows on teardown, so it must start from a fresh/migrated "
            "database. Refusing to run rather than risk deleting ambient data."
        )


async def _seed_voice_clinic(session) -> None:
    """Insert the reproducible voice-test clinic (business 1), explicitly.

    No ON CONFLICT: the precondition proved the slot empty, so ownership of every
    inserted row is unambiguous and the teardown can delete it safely.
    """
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:bid, 'Voice Test Clinic', 'dental', :phone, 'Asia/Kolkata', 'trial')"
        ),
        {"bid": _BUSINESS_ID, "phone": _OWNER_PHONE},
    )
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (:bid, :phone, 'owner', true)"
        ),
        {"bid": _BUSINESS_ID, "phone": _OWNER_PHONE},
    )
    await session.execute(
        text(
            "INSERT INTO business_whatsapp_channels "
            "(business_id, phone_number_id, status, is_primary) "
            "VALUES (:bid, :pnid, 'active', true)"
        ),
        {"bid": _BUSINESS_ID, "pnid": _WHATSAPP_PNID},
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (:sid, :bid, :name, 30, 0, 0, 500.00, true)"
        ),
        {"sid": _SERVICE_ID, "bid": _BUSINESS_ID, "name": _SERVICE_NAME},
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (:rid, :bid, :name, 'staff', true)"
        ),
        {"rid": _RESOURCE_ID, "bid": _BUSINESS_ID, "name": _RESOURCE_NAME},
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) "
            "VALUES (:bid, :sid, :rid, true)"
        ),
        {"bid": _BUSINESS_ID, "sid": _SERVICE_ID, "rid": _RESOURCE_ID},
    )
    # Open every day of the week so a booking on any target date is in-hours.
    for dow in range(0, 7):
        await session.execute(
            text(
                "INSERT INTO operating_schedules "
                "(business_id, day_of_week, open_time, close_time, is_active) "
                "VALUES (:bid, :dow, :open, :close, true)"
            ),
            {"bid": _BUSINESS_ID, "dow": dow, "open": _OPEN_TIME, "close": _CLOSE_TIME},
        )
    await session.commit()


async def _teardown_voice_clinic(session) -> None:
    """Delete every business-1 row the fixture and tests created, FK-safe.

    Runs as one transaction: on any error it rolls back and re-raises loudly so a
    partial cleanup can never silently leave guarded rows (which would surface far
    away as a downgrade-guard failure). After success, the guarded tables
    (whatsapp channels, dpdp calls, callbacks) hold zero business-1 rows.

    resource_allocations and appointment_commits are IMMUTABLE by design — a
    BEFORE DELETE trigger (migration 0004) rejects any row delete on them. That
    immutability is a production invariant we must NOT weaken; but a test's own
    ephemeral rows still have to be reclaimed. Postgres'
    ``SET LOCAL session_replication_role = replica`` suppresses user triggers for
    the DURATION OF THIS TRANSACTION ONLY: it is automatically restored to
    ``origin`` on COMMIT or ROLLBACK, so a pooled connection can never leak the
    relaxed setting to a later test. No trigger is dropped or altered and
    production behavior is untouched. (Requires the connection role to have the
    privilege — the CI Postgres user is superuser, and this helper is test-only.)
    """
    try:
        # SET LOCAL is transaction-scoped, so run it inside the same implicit
        # transaction as the deletes (before commit). Auto-reverts on commit/rollback.
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        for stmt in _TEARDOWN_STATEMENTS:
            await session.execute(text(stmt), {"bid": _BUSINESS_ID})
        await session.commit()
    except Exception:
        await session.rollback()
        raise


@pytest_asyncio.fixture
async def voice_clinic_seed():
    """Explicit, self-cleaning precondition for the voice booking tests.

    Request by name. Asserts business 1 is absent, seeds the deterministic clinic,
    yields, then removes every business-1 row (seed + test-created) so no residue
    trips the migration downgrade guards.
    """
    from fonely.core.database import async_session

    async with async_session() as session:
        await _assert_business_absent(session)
        await _seed_voice_clinic(session)
    try:
        yield
    finally:
        async with async_session() as session:
            await _teardown_voice_clinic(session)
