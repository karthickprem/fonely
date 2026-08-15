"""#43 P2 — durable owner-reply resume seam, PostgreSQL behavior.

Proves the inbound-worker owner branch resolves an owner's WhatsApp reply against
an outstanding voice-call wait (a durable ``awaiting_owner_reply`` pending_actions
marker): the cardinality gate (0/1/>1), exact code correlation, the CAS to
RESUME_REQUESTED, bounded answer persistence, the DB-durable code-guess limit, and
the critical predicate split (a RESUME_REQUESTED marker is never re-selected or
re-answered). Tenant isolation is asserted throughout.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.models.enums import PendingActionStatus
from fonely.services.owner_reply_resume import (
    DisambiguationReminder,
    FallThrough,
    OwnerReplyResumeService,
    Resumed,
)

pytestmark = pytest.mark.postgres


async def _seed_business_owner_call(session: AsyncSession, business_id: int) -> tuple[str, int]:
    owner_phone = f"+9190000000{business_id:02d}"
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:id, :name, 'dental', :phone, 'Asia/Kolkata', 'trial') "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": business_id, "name": f"Clinic {business_id}", "phone": owner_phone},
    )
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (:bid, :phone, 'owner', true) ON CONFLICT DO NOTHING"
        ),
        {"bid": business_id, "phone": owner_phone},
    )
    row = await session.execute(
        text(
            "INSERT INTO calls (business_id, caller_phone, caller_role, started_at) "
            "VALUES (:bid, '+919000000000', 'customer', now()) RETURNING id"
        ),
        {"bid": business_id},
    )
    call_id = int(row.scalar_one())
    await session.flush()
    return owner_phone, call_id


async def _insert_marker(
    session: AsyncSession,
    *,
    business_id: int,
    call_id: int,
    code: str,
    query_type: str = "availability",
    status: str = "awaiting_owner_reply",
    expires_in_minutes: int = 60,
    idem: str | None = None,
) -> int:
    # Build the marker payload the way Dev4's P1 does: a validated envelope whose
    # canonical form + digest are consistent (so a validated read succeeds until
    # the answer CAS deliberately keeps them consistent).
    from fonely.domain.pending_actions.payloads import validate_payload
    from fonely.domain.pending_actions.snapshots import (
        canonical_payload_dict,
        payload_digest,
    )
    from fonely.models.enums import PendingActionType

    envelope = validate_payload(
        PendingActionType.AWAITING_OWNER_REPLY,
        1,
        {
            "schema_version": 1,
            "action_type": "awaiting_owner_reply",
            "data": {
                "answer_text": None,
                "answered_by_phone": None,
                "answered_at": None,
                "answer_unused": False,
            },
        },
    )
    payload = json.dumps(canonical_payload_dict(envelope))
    row = await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(business_id, action_type, payload_schema_version, proposed_payload, "
            " payload_digest, status, expires_at, idempotency_key, initiated_by, "
            " call_id, query_type, correlation_code, version) "
            "VALUES (:bid, 'awaiting_owner_reply', 1, CAST(:payload AS jsonb), :digest, "
            " :status, now() + make_interval(mins => :mins), :idem, '+919000000000', "
            " :call_id, :qt, :code, 1) RETURNING id"
        ),
        {
            "bid": business_id,
            "payload": payload,
            "digest": payload_digest(envelope),
            "status": status,
            "mins": expires_in_minutes,
            "idem": idem or f"marker-{business_id}-{call_id}-{code}",
            "call_id": call_id,
            "qt": query_type,
            "code": code,
        },
    )
    await session.flush()
    return int(row.scalar_one())


async def _status(session: AsyncSession, action_id: int) -> str:
    return await session.scalar(
        text("SELECT status FROM pending_actions WHERE id = :id"), {"id": action_id}
    )


async def _answer(session: AsyncSession, action_id: int) -> dict:
    payload = await session.scalar(
        text("SELECT proposed_payload FROM pending_actions WHERE id = :id"), {"id": action_id}
    )
    return payload["data"]


# --- 1. No marker -> fall through -------------------------------------------
async def test_no_marker_falls_through(pg_session: AsyncSession) -> None:
    owner, _ = await _seed_business_owner_call(pg_session, 1)
    out = await OwnerReplyResumeService(pg_session).try_resolve_owner_reply(
        business_id=1, owner_phone=owner, message_text="today leave"
    )
    assert isinstance(out, FallThrough)


# --- 2. Exactly one marker, no code -> resolve ------------------------------
async def test_single_marker_no_code_resolves(pg_session: AsyncSession) -> None:
    owner, call_id = await _seed_business_owner_call(pg_session, 1)
    mid = await _insert_marker(pg_session, business_id=1, call_id=call_id, code="ABCD2345")
    out = await OwnerReplyResumeService(pg_session).try_resolve_owner_reply(
        business_id=1, owner_phone=owner, message_text="yes she is free at 6:30"
    )
    assert isinstance(out, Resumed)
    assert await _status(pg_session, mid) == PendingActionStatus.RESUME_REQUESTED.value
    ans = await _answer(pg_session, mid)
    assert ans["answer_text"] == "yes she is free at 6:30"
    assert ans["answered_by_phone"] == owner


async def _validated_read(session: AsyncSession, action_id: int):
    """P3's read path: validate the stored payload and enforce digest integrity —
    the same check _validated_stored_payload runs (raises on mismatch)."""
    from fonely.domain.pending_actions.payloads import validate_payload
    from fonely.domain.pending_actions.snapshots import payload_digest
    from fonely.models.enums import PendingActionType
    from fonely.models.schema import PendingAction
    from sqlalchemy import select as _select

    action = (
        await session.execute(_select(PendingAction).where(PendingAction.id == action_id))
    ).scalar_one()
    payload = validate_payload(
        PendingActionType(action.action_type),
        action.payload_schema_version,
        action.proposed_payload,
    )
    if payload_digest(payload) != action.payload_digest:
        raise AssertionError("Stored payload digest mismatch")
    return payload


# --- 2b. Digest stays consistent after the answer CAS (P3 validated read) ----
async def test_answer_cas_keeps_payload_digest_consistent(pg_session: AsyncSession) -> None:
    owner, call_id = await _seed_business_owner_call(pg_session, 1)
    mid = await _insert_marker(pg_session, business_id=1, call_id=call_id, code="ABCD2345")
    # Precondition: the freshly-created marker already reads valid.
    await _validated_read(pg_session, mid)

    out = await OwnerReplyResumeService(pg_session).try_resolve_owner_reply(
        business_id=1, owner_phone=owner, message_text="yes free at 6:30"
    )
    assert isinstance(out, Resumed)

    # THE FIX: after the answer CAS, the validated read (P3's path) must still
    # succeed — payload_digest was recomputed atomically with the payload mutation.
    payload = await _validated_read(pg_session, mid)
    assert payload.data.answer_text == "yes free at 6:30"
    assert payload.data.answered_by_phone == owner


async def test_stale_digest_would_fail_validated_read(pg_session: AsyncSession) -> None:
    # Negative control: prove the validated read actually enforces the digest, so
    # the positive test above is meaningful (not a no-op). Mutate the payload WITHOUT
    # updating the digest -> the validated read raises.
    owner, call_id = await _seed_business_owner_call(pg_session, 1)
    mid = await _insert_marker(pg_session, business_id=1, call_id=call_id, code="ABCD2345")
    await pg_session.execute(
        text(
            "UPDATE pending_actions SET proposed_payload = "
            "jsonb_set(proposed_payload, '{data,answer_text}', '\"tampered\"') "
            "WHERE id = :id"
        ),
        {"id": mid},
    )
    with pytest.raises(AssertionError, match="digest mismatch"):
        await _validated_read(pg_session, mid)


# --- 3. Two markers, no code -> reminder, resume none -----------------------
async def test_two_markers_no_code_reminds_and_resumes_none(pg_session: AsyncSession) -> None:
    owner, c1 = await _seed_business_owner_call(pg_session, 1)
    _, c2 = await _seed_business_owner_call(pg_session, 1)  # second call, same business
    m1 = await _insert_marker(pg_session, business_id=1, call_id=c1, code="AAAA2222", idem="m1")
    m2 = await _insert_marker(pg_session, business_id=1, call_id=c2, code="BBBB3333", idem="m2")
    out = await OwnerReplyResumeService(pg_session).try_resolve_owner_reply(
        business_id=1, owner_phone=owner, message_text="yes free"
    )
    assert isinstance(out, DisambiguationReminder)
    assert "AAAA2222" in out.text and "BBBB3333" in out.text
    assert await _status(pg_session, m1) == PendingActionStatus.AWAITING_OWNER_REPLY.value
    assert await _status(pg_session, m2) == PendingActionStatus.AWAITING_OWNER_REPLY.value


# --- 3b. Two markers, correct code -> resolves ONLY that one -----------------
async def test_two_markers_code_resolves_exactly_one(pg_session: AsyncSession) -> None:
    owner, c1 = await _seed_business_owner_call(pg_session, 1)
    _, c2 = await _seed_business_owner_call(pg_session, 1)
    m1 = await _insert_marker(pg_session, business_id=1, call_id=c1, code="AAAA2222", idem="m1")
    m2 = await _insert_marker(pg_session, business_id=1, call_id=c2, code="BBBB3333", idem="m2")
    out = await OwnerReplyResumeService(pg_session).try_resolve_owner_reply(
        business_id=1, owner_phone=owner, message_text="[BBBB3333] yes at 6:30"
    )
    assert isinstance(out, Resumed)
    assert await _status(pg_session, m1) == PendingActionStatus.AWAITING_OWNER_REPLY.value
    assert await _status(pg_session, m2) == PendingActionStatus.RESUME_REQUESTED.value
    # Code stripped from the persisted answer.
    assert (await _answer(pg_session, m2))["answer_text"] == "yes at 6:30"


# --- 4. Tenant isolation: business A's code never resolves B's marker -------
async def test_tenant_isolation_code_scoped(pg_session: AsyncSession) -> None:
    owner1, c1 = await _seed_business_owner_call(pg_session, 1)
    _owner2, c2 = await _seed_business_owner_call(pg_session, 2)
    # Both businesses have a marker; business 2's uses code SAME2345.
    m1 = await _insert_marker(pg_session, business_id=1, call_id=c1, code="SAME2345", idem="m1")
    m2 = await _insert_marker(pg_session, business_id=2, call_id=c2, code="SAME2345", idem="m2")
    # Owner of business 1 replies with that code — resolves ONLY business 1's marker.
    out = await OwnerReplyResumeService(pg_session).try_resolve_owner_reply(
        business_id=1, owner_phone=owner1, message_text="code: SAME2345 yes"
    )
    assert isinstance(out, Resumed)
    assert await _status(pg_session, m1) == PendingActionStatus.RESUME_REQUESTED.value
    assert await _status(pg_session, m2) == PendingActionStatus.AWAITING_OWNER_REPLY.value


# --- 5. Expired marker -> not selectable ------------------------------------
async def test_expired_marker_not_resolved(pg_session: AsyncSession) -> None:
    owner, call_id = await _seed_business_owner_call(pg_session, 1)
    mid = await _insert_marker(
        pg_session, business_id=1, call_id=call_id, code="EXPD3333", expires_in_minutes=-1
    )
    out = await OwnerReplyResumeService(pg_session).try_resolve_owner_reply(
        business_id=1, owner_phone=owner, message_text="EXPD3333"
    )
    # A CODED reply whose marker is expired is resume-looking → reminder, NEVER a
    # fall-through to a generic owner command (the frozen guard). Marker untouched.
    assert isinstance(out, DisambiguationReminder)
    assert await _status(pg_session, mid) == PendingActionStatus.AWAITING_OWNER_REPLY.value


# --- 6. RESUME_REQUESTED marker is not re-selectable (predicate split) -------
async def test_resume_requested_marker_not_reanswered(pg_session: AsyncSession) -> None:
    owner, call_id = await _seed_business_owner_call(pg_session, 1)
    mid = await _insert_marker(
        pg_session, business_id=1, call_id=call_id, code="DKNE4444", status="resume_requested"
    )
    # It's already answered/in the claim pipeline (RESUME_REQUESTED). A coded reply
    # to it is a stale no-op: get_by_code sees it as NOT selectable → reminder
    # (never re-CAS'd, never fallen through to an availability mutation).
    out = await OwnerReplyResumeService(pg_session).try_resolve_owner_reply(
        business_id=1, owner_phone=owner, message_text="DKNE4444"
    )
    assert isinstance(out, DisambiguationReminder)
    assert await _status(pg_session, mid) == PendingActionStatus.RESUME_REQUESTED.value


# --- 7. Wrong code with an ACTIVE marker present -> reminder + guess counted -
async def test_wrong_code_reminds_and_counts(pg_session: AsyncSession) -> None:
    owner, call_id = await _seed_business_owner_call(pg_session, 1)
    mid = await _insert_marker(pg_session, business_id=1, call_id=call_id, code="RIGH5555")
    svc = OwnerReplyResumeService(pg_session)
    out = await svc.try_resolve_owner_reply(
        business_id=1, owner_phone=owner, message_text="code: WRNG9999"
    )
    assert isinstance(out, DisambiguationReminder)
    # Marker untouched; a guess was recorded (DB-durable).
    assert await _status(pg_session, mid) == PendingActionStatus.AWAITING_OWNER_REPLY.value
    n = await pg_session.scalar(
        text(
            "SELECT attempts FROM owner_reply_guess_attempts "
            "WHERE business_id = 1 AND owner_phone = :p"
        ),
        {"p": owner},
    )
    assert n == 1


# --- 8. Rate limit: over max wrong codes -> refused (DB-durable) -------------
async def test_wrong_code_rate_limited(pg_session: AsyncSession) -> None:
    owner, call_id = await _seed_business_owner_call(pg_session, 1)
    await _insert_marker(pg_session, business_id=1, call_id=call_id, code="RIGH5555")
    svc = OwnerReplyResumeService(pg_session)
    # 5 wrong guesses are allowed (each a reminder); the 6th trips the limit.
    for _ in range(5):
        out = await svc.try_resolve_owner_reply(
            business_id=1, owner_phone=owner, message_text="code: WRNG9999"
        )
        assert isinstance(out, DisambiguationReminder)
    out6 = await svc.try_resolve_owner_reply(
        business_id=1, owner_phone=owner, message_text="code: WRNG9999"
    )
    assert isinstance(out6, DisambiguationReminder)
    assert "Too many" in out6.text


# --- 9. Bounded answer: over-500 answer is truncated, not rejected outright --
async def test_answer_bounded_to_500(pg_session: AsyncSession) -> None:
    owner, call_id = await _seed_business_owner_call(pg_session, 1)
    mid = await _insert_marker(pg_session, business_id=1, call_id=call_id, code="LNKG6666")
    long_answer = "x" * 900
    out = await OwnerReplyResumeService(pg_session).try_resolve_owner_reply(
        business_id=1, owner_phone=owner, message_text=long_answer
    )
    assert isinstance(out, Resumed)
    assert len((await _answer(pg_session, mid))["answer_text"]) == 500


# --- 10. CAS race / exactly-once across independent sessions -----------------
async def test_cas_exactly_once_under_race(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s:
        owner, call_id = await _seed_business_owner_call(s, 1)
        mid = await _insert_marker(s, business_id=1, call_id=call_id, code="RACE7777")
        await s.commit()

    barrier = asyncio.Barrier(2)

    async def reply() -> object:
        async with pg_session_factory() as s:
            svc = OwnerReplyResumeService(s)
            await barrier.wait()
            out = await svc.try_resolve_owner_reply(
                business_id=1, owner_phone=owner, message_text="RACE7777"
            )
            await s.commit()
            return out

    results = await asyncio.gather(reply(), reply())
    resumed = [r for r in results if isinstance(r, Resumed)]
    reminders = [r for r in results if isinstance(r, DisambiguationReminder)]
    # Exactly one resolves; the loser sees the marker already resolved.
    assert len(resumed) == 1
    assert len(reminders) == 1
    async with pg_session_factory() as s:
        assert await _status(s, mid) == PendingActionStatus.RESUME_REQUESTED.value
