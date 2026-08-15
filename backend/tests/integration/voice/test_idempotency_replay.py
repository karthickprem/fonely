"""#45(c) PG: the deterministic KEY routes a retried propose to the dedup path.

The #45(c) fix is the KEY determinism (id(self) → a pure function of durable
trusted values), proven restart-stable by the unit tests. This PG test asserts
the HONEST thing the key ALONE delivers: a retried propose with the same
semantic key is RECOGNIZED as the same idempotency lineage (routed to the dedup
path, no second independent pending action), which a non-deterministic key could
not do.

It deliberately does NOT assert a full propose-REPLAY (same proposal returned),
because two known SEPARATE, non-voice issues currently prevent it:
  1. backend_ports.propose recomputes expires_at=now()+15 per call, and
     pending_actions.py:824 WRONGLY includes expires_at in the idempotency
     IDENTITY equality — so a retry conflicts instead of replaying. Removing
     expires_at from that identity is a SHARED-SERVICE fix (pending_actions.py),
     filed separately — NOT voice-owned, NOT part of #45(c). (started_at+15 was
     REJECTED as a voice-side anchor: it is born-expired for calls running >15min
     — pending_actions.py:836 — so there is no voice-owned deterministic anchor
     that preserves the real TTL.)
  2. A full propose+confirm booking retry against an ALREADY-CONFIRMED action
     hits appointments.py:907 _assert_semantic_equivalence before the
     terminal-replay branch and conflicts — also a separate appointments-service
     question.
Asserting a full replay here would be a false claim about what the key alone
delivers. The key is the necessary voice-side half.

NOT a unit test — requires PostgreSQL with the full clinic seed (business_id=1,
'scaling' service, resource_id=1, eligibility).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
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
from fonely.voice.frame_pipeline import _voice_idempotency_key  # noqa: E402
from fonely.voice.runtime import ProposeCommand, TrustedCommandContext  # noqa: E402


@asynccontextmanager
async def _session_factory():
    from fonely.core.database import async_session

    async with async_session() as session:
        yield session


def _validation_factory(session):
    from fonely.api.internal.validation import InternalValidationPort

    return InternalValidationPort(session)


async def _ensure_whatsapp_channel(business_id: int) -> None:
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
                {"b": business_id, "pnid": f"replay-pnid-{business_id}"},
            )
            await s.commit()


async def _seed_calls_row(business_id: int) -> int:
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
        cid = r.scalar()
        await s.commit()
    return int(cid)


async def _cleanup_key(business_id: int, key: str) -> None:
    """Free this test's pending action (and any appointment) for its unique
    idempotency key, so repeated runs against a persistent DB don't accumulate
    held-slot capacity. The key is unique per run (call_id-derived), so this
    only ever touches this run's own rows."""
    from sqlalchemy import text as sql_text

    from fonely.core.database import async_session

    pa_ids = "SELECT id FROM pending_actions WHERE business_id = :b AND idempotency_key = :k"
    appt_ids = (
        f"SELECT id FROM appointments WHERE business_id = :b AND pending_action_id IN ({pa_ids})"
    )
    params = {"b": business_id, "k": key}
    async with async_session() as s:
        # resource_allocations FK appointments; appointments FK pending_actions.
        # Delete in dependency order so no FK violation.
        await s.execute(
            sql_text(f"DELETE FROM resource_allocations WHERE appointment_id IN ({appt_ids})"),
            params,
        )
        await s.execute(
            sql_text(
                "DELETE FROM appointments WHERE business_id = :b "
                f"AND pending_action_id IN ({pa_ids})"
            ),
            params,
        )
        await s.execute(
            sql_text("DELETE FROM pending_actions WHERE business_id = :b AND idempotency_key = :k"),
            params,
        )
        await s.commit()


def _port(*, business_id: int, call_id: int) -> AppointmentServiceCommandPort:
    actor = build_actor_context(
        business_id=business_id, phone="+919000000000", session_id=f"replay-{call_id}"
    )
    return AppointmentServiceCommandPort(
        actor=actor,
        session_factory=_session_factory,
        validation_factory=_validation_factory,
        business_timezone="Asia/Kolkata",
        conversation_id=f"replay-conv-{call_id}",
        call_id=call_id,
    )


def _propose_cmd(*, business_id: int, call_id: int, key: str, target_date: date):
    return ProposeCommand(
        context=TrustedCommandContext(
            business_id=business_id,
            actor_session_id=f"replay-{call_id}",
            conversation_id=f"replay-conv-{call_id}",
        ),
        service_id=None,
        resource_id=1,
        target_date=target_date,
        target_time="17:00",
        idempotency_key=key,
    )


class TestSemanticKeyRoutesRetryToDedup:
    @pytest.mark.asyncio
    async def test_same_key_retry_is_recognized_as_the_same_idempotency_lineage(
        self, voice_clinic_seed
    ):
        """What the KEY fix alone delivers (honestly scoped): a retried propose
        with the same semantic key is RECOGNIZED as the same idempotency key —
        routed to the dedup path, NOT creating a second independent pending
        action. It does NOT yet fully REPLAY, because backend_ports.propose still
        recomputes expires_at=now()+15 per call and pending_actions.py:824
        wrongly includes expires_at in the idempotency IDENTITY equality — so the
        retry conflicts instead of replaying. Removing expires_at from that
        identity is a SEPARATE SHARED-SERVICE fix (pending_actions.py, filed
        separately); it is NOT voice-owned and NOT part of #45(c). The key fix is
        the necessary voice-side half: without a deterministic key the retry
        wouldn't even reach the dedup path.
        """
        business_id = 1
        call_id = await _seed_calls_row(business_id)
        target_date = date(2026, 9, 1) + timedelta(days=call_id % 300)

        key = _voice_idempotency_key(
            business_id=business_id,
            call_id=call_id,
            target_date=target_date,
            target_time=dt_time(17, 0),
            resource_id=1,
        )

        await _cleanup_key(business_id, key)  # clear any prior-run leftover for this key

        port = _port(business_id=business_id, call_id=call_id)
        first = await port.propose(
            _propose_cmd(business_id=business_id, call_id=call_id, key=key, target_date=target_date)
        )
        assert first.success, first.error
        assert first.proposal_id is not None

        # The retry with the SAME key reaches the dedup path (proven by the
        # idempotency-conflict outcome, which ONLY fires because the key matched —
        # a non-deterministic key would have created a second action instead).
        second = await port.propose(
            _propose_cmd(business_id=business_id, call_id=call_id, key=key, target_date=target_date)
        )
        assert not second.success
        assert "Idempotency" in second.error  # recognized as the same key lineage

        # ANTI-DOUBLE-BOOK: no SECOND independent pending action was created for
        # this key — exactly ONE exists (the first). The conflict prevented a
        # duplicate; the key fix is what routed the retry here.
        from sqlalchemy import text as sql_text

        from fonely.core.database import async_session

        async with async_session() as s:
            r = await s.execute(
                sql_text(
                    "SELECT count(*) FROM pending_actions "
                    "WHERE business_id = :b AND idempotency_key = :k"
                ),
                {"b": business_id, "k": key},
            )
            assert r.scalar() == 1  # one action for the key — no duplicate

    # NOTE: cross-call key DISTINCTNESS (two different calls → different keys →
    # distinct attempts) is proven deterministically, with no DB and no slot
    # contention, by the unit test test_idempotency_key.py::
    # TestKeyDistinctions::test_different_call_same_slot_different_keys. A PG
    # version here would only re-prove the pure-function property while adding
    # shared-DB slot-capacity flakiness, so it is deliberately left to the unit
    # test — the PG test above proves the one thing that genuinely needs the DB:
    # the key routes a retry to the service dedup path.
