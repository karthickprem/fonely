"""#45(b): a voice booking is provably traceable back to its originating call.

The appointment (and its pending action) persist the admitted call's id, and the
(business_id, call_id) FK resolves to the real calls row — so provenance is REAL,
not just a populated column. call_id originates ONLY from the admitted session
(threaded at command_port construction), never from the propose command's
caller/model fields.

NOT a unit test — requires PostgreSQL with the seeded schema.
    DATABASE_URL=postgresql+asyncpg://localhost:5432/fonely \
        pytest -m postgres tests/integration/voice/test_call_provenance.py -v
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date
from datetime import time as dt_time

import pytest

pytestmark = pytest.mark.postgres


def _db_available() -> bool:
    return "postgresql" in os.environ.get("DATABASE_URL", "")


if not _db_available():
    pytest.skip("PostgreSQL not available", allow_module_level=True)


from fonely.voice.backend_ports import (  # noqa: E402
    AppointmentServiceCommandPort,
    build_actor_context,
)
from fonely.voice.clinic_resolver import book_appointment  # noqa: E402


@asynccontextmanager
async def _session_factory():
    from fonely.core.database import async_session

    async with async_session() as session:
        yield session


def _validation_factory(session):
    from fonely.api.internal.validation import InternalValidationPort

    return InternalValidationPort(session)


async def _ensure_whatsapp_channel(business_id: int) -> None:
    """The confirm/commit path creates appointment notifications, which require
    an active WhatsApp channel for the business. Seed one idempotently so the
    full commit completes — the notification infra is not what this test proves,
    but the commit must reach the appointment row."""
    from sqlalchemy import text as sql_text

    from fonely.core.database import async_session

    async with async_session() as s:
        exists = await s.execute(
            sql_text(
                "SELECT 1 FROM business_whatsapp_channels "
                "WHERE business_id = :b AND status = 'active' LIMIT 1"
            ),
            {"b": business_id},
        )
        if exists.first() is None:
            await s.execute(
                sql_text(
                    "INSERT INTO business_whatsapp_channels "
                    "(business_id, phone_number_id, status, is_primary) "
                    "VALUES (:b, :pnid, 'active', true)"
                ),
                {"b": business_id, "pnid": f"prov-test-pnid-{business_id}"},
            )
            await s.commit()


async def _seed_calls_row(business_id: int) -> int:
    """Insert a calls row (the admitted call the booking traces back to) and
    return its id, so the (business_id, call_id) FK has a real target."""
    from sqlalchemy import text as sql_text

    from fonely.core.database import async_session

    await _ensure_whatsapp_channel(business_id)
    async with async_session() as s:
        r = await s.execute(
            sql_text(
                "INSERT INTO calls (business_id, caller_phone, caller_role, started_at) "
                "VALUES (:b, :p, 'customer', now()) RETURNING id"
            ),
            {"b": business_id, "p": "+919000000000"},
        )
        call_id = r.scalar()
        await s.commit()
    return int(call_id)


def _port(*, business_id: int, call_id: int | None) -> AppointmentServiceCommandPort:
    actor = build_actor_context(
        business_id=business_id, phone="+919000000000", session_id=f"prov-{call_id}"
    )
    return AppointmentServiceCommandPort(
        actor=actor,
        session_factory=_session_factory,
        validation_factory=_validation_factory,
        business_timezone="Asia/Kolkata",
        conversation_id=f"prov-conv-{call_id}",
        call_id=call_id,
    )


async def _book(
    port: AppointmentServiceCommandPort,
    business_id: int,
    call_id: int,
    *,
    target_time: dt_time,
):
    # Key AND date derived from the seeded call_id (calls rows autoincrement), so
    # every test run books a UNIQUE future slot no prior run touched — no
    # capacity contention, no deletion fighting referential integrity. This test
    # proves provenance, not concurrency; a fresh slot per run isolates it.
    from datetime import timedelta

    from fonely.core.database import async_session

    target_date = date(2026, 9, 1) + timedelta(days=call_id % 60)
    async with async_session() as session:
        return await book_appointment(
            command_port=port,
            session=session,
            business_id=business_id,
            service_phrase="scaling",
            target_date=target_date,
            target_time=target_time,
            idempotency_key=f"prov-{call_id}",
            resource_id=1,
        )


async def _appointment_call_id(appointment_id: int) -> int | None:
    from sqlalchemy import text as sql_text

    from fonely.core.database import async_session

    async with async_session() as s:
        r = await s.execute(
            sql_text("SELECT call_id FROM appointments WHERE id = :id"),
            {"id": appointment_id},
        )
        row = r.fetchone()
    return row[0] if row else None


class TestCallProvenance:
    @pytest.mark.asyncio
    async def test_committed_appointment_carries_the_admitted_call_id(self, voice_clinic_seed):
        business_id = 1
        call_id = await _seed_calls_row(business_id)
        outcome = await _book(
            _port(business_id=business_id, call_id=call_id),
            business_id,
            call_id,
            target_time=dt_time(17, 15),
        )

        assert outcome.success, outcome.error
        stored = await _appointment_call_id(outcome.appointment_id)
        assert stored == call_id  # PROVENANCE: appointment links to the admitted call

    @pytest.mark.asyncio
    async def test_fk_pair_resolves_to_the_real_calls_row(self, voice_clinic_seed):
        # The (business_id, call_id) pair must reference the ACTUAL admitted call
        # row — provenance being REAL, not a call_id pointing at nothing/wrong biz.
        from sqlalchemy import text as sql_text

        from fonely.core.database import async_session

        business_id = 1
        call_id = await _seed_calls_row(business_id)
        outcome = await _book(
            _port(business_id=business_id, call_id=call_id),
            business_id,
            call_id,
            target_time=dt_time(17, 30),
        )
        assert outcome.success, outcome.error

        async with async_session() as s:
            r = await s.execute(
                sql_text(
                    "SELECT c.id, c.business_id FROM appointments a "
                    "JOIN calls c ON c.business_id = a.business_id AND c.id = a.call_id "
                    "WHERE a.id = :id"
                ),
                {"id": outcome.appointment_id},
            )
            row = r.fetchone()
        assert row is not None, "the (business_id, call_id) FK does not resolve to a calls row"
        assert row[0] == call_id
        assert row[1] == business_id  # resolves to the admitted call, right business

    @pytest.mark.asyncio
    async def test_confirmed_appointment_inherits_propose_time_call_id(self, voice_clinic_seed):
        # The confirm path carries no call_id (works off pending_action_id); the
        # confirmed appointment inherits it from the pending action set at
        # propose. Proven, not assumed.
        business_id = 1
        call_id = await _seed_calls_row(business_id)
        outcome = await _book(
            _port(business_id=business_id, call_id=call_id),
            business_id,
            call_id,
            target_time=dt_time(17, 45),
        )
        assert outcome.success, outcome.error
        # The committed (confirmed) appointment's call_id is the propose-time one.
        assert await _appointment_call_id(outcome.appointment_id) == call_id
