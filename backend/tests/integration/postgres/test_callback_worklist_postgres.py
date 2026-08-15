"""Owner-facing callback worklist (#41 part B): tenant-scoped query + resolve.

Proves the owner surface over #36's durable callbacks:
  * list_pending returns ONLY the acting owner's business's pending callbacks
    (tenant isolation — the load-bearing invariant), with the partial facts;
  * resolve transitions a callback to terminal CANCELLED, attributes it to the
    owner, and stops it surfacing — on EXISTING columns, no schema change;
  * a non-owner cannot query or resolve;
  * a foreign-tenant / non-callback / already-resolved id is refused, not
    silently actioned;
  * concurrency: a stale version loses the resolve race.

This is B — QUERY + RESOLVE only. It does NOT push callbacks to the owner (that
is A, gated on migration 0020 + founder auth). So these tests assert the owner
can PULL and act, NOT that they are notified.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Import-origin guard: exercise THIS checkout's src (portable, not a branch name).
import fonely.services.callback_worklist as _worklist_mod
from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.errors import (
    InvalidStateTransitionError,
    PendingActionConcurrencyError,
    PendingActionNotFoundError,
    PendingActionUnauthorizedError,
)
from fonely.models.enums import CallerRole, Channel
from fonely.services.callback_worklist import CallbackWorklistService
from tests.integration.postgres.import_origin import assert_module_from_this_checkout

assert_module_from_this_checkout(_worklist_mod, __file__)

pytestmark = pytest.mark.postgres


def _owner(business_id: int, phone: str = "+919000000001") -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone=phone,
        verified_role=CallerRole.OWNER,
        channel=Channel.TEXT,
    )


def _customer(business_id: int) -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.VOICE,
    )


async def _seed_business_with_owner(session: AsyncSession, business_id: int) -> str:
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
            "VALUES (:bid, :phone, 'owner', true)"
        ),
        {"bid": business_id, "phone": owner_phone},
    )
    return owner_phone


async def _insert_callback(
    session: AsyncSession,
    *,
    business_id: int,
    idempotency_key: str,
    status: str = "collecting_details",
    caller_phone: str = "+919123456789",
    reason_code: str = "doctor_disambiguation_exhausted",
) -> int:
    payload = (
        '{"schema_version": 1, "action_type": "callback", "data": {'
        f'"reason_code": "{reason_code}", "caller_phone": "{caller_phone}", '
        '"service_id": 1, "service_name": "General Consultation", '
        '"target_date": "2026-08-20", '
        '"attempted_candidates": ["Dr. Priya Kumar", "Dr. Priya Rao"], '
        '"requested_at": "2026-08-15T09:00:00Z"}}'
    )
    result = await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(business_id, action_type, payload_schema_version, proposed_payload, "
            " payload_digest, status, expires_at, idempotency_key, initiated_by, version) "
            "VALUES (:bid, 'callback', 1, CAST(:payload AS jsonb), :digest, :status, "
            " now() + interval '1 hour', :idem, :caller, 1) "
            "RETURNING id"
        ),
        {
            "bid": business_id,
            "payload": payload,
            "digest": f"digest-{idempotency_key}",
            "status": status,
            "idem": idempotency_key,
            "caller": caller_phone,
        },
    )
    await session.flush()
    return int(result.scalar_one())


async def test_list_pending_is_tenant_scoped(pg_session: AsyncSession) -> None:
    # Business 1 has two pending callbacks; business 2 has one. Owner of 1 must
    # see ONLY business 1's two — never business 2's.
    owner1 = await _seed_business_with_owner(pg_session, 1)
    owner2 = await _seed_business_with_owner(pg_session, 2)
    await _insert_callback(pg_session, business_id=1, idempotency_key="b1-a")
    await _insert_callback(pg_session, business_id=1, idempotency_key="b1-b")
    await _insert_callback(pg_session, business_id=2, idempotency_key="b2-a")

    svc = CallbackWorklistService(pg_session)
    items = await svc.list_pending(_owner(1, owner1))

    assert len(items) == 2, "owner of business 1 must see exactly their two callbacks"
    assert all(i.reason_code == "doctor_disambiguation_exhausted" for i in items)
    # The partial facts a human needs to resume are surfaced.
    assert items[0].caller_phone == "+919123456789"
    assert items[0].service_name == "General Consultation"
    assert items[0].target_date == "2026-08-20"
    assert items[0].attempted_candidates == ("Dr. Priya Kumar", "Dr. Priya Rao")

    # And business 2's owner sees only business 2's one.
    other = await svc.list_pending(_owner(2, owner2))
    assert len(other) == 1


async def test_list_excludes_resolved_callbacks(pg_session: AsyncSession) -> None:
    await _seed_business_with_owner(pg_session, 1)
    await _insert_callback(pg_session, business_id=1, idempotency_key="open")
    await _insert_callback(pg_session, business_id=1, idempotency_key="done", status="cancelled")

    items = await CallbackWorklistService(pg_session).list_pending(_owner(1))
    assert len(items) == 1, "a resolved (cancelled) callback must not surface"


async def test_resolve_transitions_to_terminal_and_attributes_owner(
    pg_session: AsyncSession,
) -> None:
    owner_phone = await _seed_business_with_owner(pg_session, 1)
    cb_id = await _insert_callback(pg_session, business_id=1, idempotency_key="to-resolve")

    svc = CallbackWorklistService(pg_session)
    item = await svc.resolve(_owner(1, owner_phone), cb_id, expected_version=1)

    assert item.status == "cancelled", "resolve must move the callback to terminal cancelled"

    # Persisted: terminal status, owner attribution, and it stops surfacing.
    row = (
        await pg_session.execute(
            text(
                "SELECT status, confirmed_by, confirmed_at, rejection_reason_code "
                "FROM pending_actions WHERE id = :id"
            ),
            {"id": cb_id},
        )
    ).one()
    assert row[0] == "cancelled"
    assert row[1] == owner_phone
    assert row[2] is not None
    assert row[3] == "owner_handled"

    remaining = await svc.list_pending(_owner(1, owner_phone))
    assert all(i.pending_action_id != cb_id for i in remaining), (
        "a resolved callback must no longer appear in the worklist"
    )


async def test_double_resolve_fails_closed(pg_session: AsyncSession) -> None:
    owner_phone = await _seed_business_with_owner(pg_session, 1)
    cb_id = await _insert_callback(pg_session, business_id=1, idempotency_key="dbl")
    svc = CallbackWorklistService(pg_session)

    resolved = await svc.resolve(_owner(1, owner_phone), cb_id, expected_version=1)
    assert resolved.status == "cancelled"

    # Second resolve: the callback is terminal now. Fail closed (invalid
    # transition), never a silent no-op that would read as success.
    with pytest.raises(InvalidStateTransitionError):
        await svc.resolve(_owner(1, owner_phone), cb_id, expected_version=2)


async def test_non_owner_cannot_query_or_resolve(pg_session: AsyncSession) -> None:
    await _seed_business_with_owner(pg_session, 1)
    cb_id = await _insert_callback(pg_session, business_id=1, idempotency_key="guarded")
    svc = CallbackWorklistService(pg_session)

    # A customer (no owner BusinessUser membership) is refused on both paths.
    with pytest.raises(PendingActionUnauthorizedError):
        await svc.list_pending(_customer(1))
    with pytest.raises(PendingActionUnauthorizedError):
        await svc.resolve(_customer(1), cb_id, expected_version=1)


async def test_foreign_tenant_resolve_is_not_found(pg_session: AsyncSession) -> None:
    # Business 1's callback must be invisible AND unresolvable to business 2's
    # owner — the same tenant-isolation invariant as list, on the mutate path.
    await _seed_business_with_owner(pg_session, 1)
    owner2_phone = await _seed_business_with_owner(pg_session, 2)
    cb_id = await _insert_callback(pg_session, business_id=1, idempotency_key="b1-only")

    svc = CallbackWorklistService(pg_session)
    with pytest.raises(PendingActionNotFoundError):
        await svc.resolve(_owner(2, owner2_phone), cb_id, expected_version=1)

    # And it is untouched — still pending for business 1.
    status = await pg_session.scalar(
        text("SELECT status FROM pending_actions WHERE id = :id"), {"id": cb_id}
    )
    assert status == "collecting_details", "a foreign-tenant resolve must not mutate the row"


async def test_resolve_stale_version_loses(pg_session: AsyncSession) -> None:
    owner_phone = await _seed_business_with_owner(pg_session, 1)
    cb_id = await _insert_callback(pg_session, business_id=1, idempotency_key="ver")
    svc = CallbackWorklistService(pg_session)

    # A wrong expected_version (the row is at 1) loses the optimistic-concurrency
    # check — a stale worklist cannot resolve a row that moved under it.
    with pytest.raises(PendingActionConcurrencyError):
        await svc.resolve(_owner(1, owner_phone), cb_id, expected_version=999)

    status = await pg_session.scalar(
        text("SELECT status FROM pending_actions WHERE id = :id"), {"id": cb_id}
    )
    assert status == "collecting_details", "a stale-version resolve must not mutate the row"


async def test_resolve_non_callback_id_is_not_found(pg_session: AsyncSession) -> None:
    # An id that is not a callback (e.g. an appointment PA) must not be resolvable
    # through the callback worklist — action_type is checked, not just tenant.
    await _seed_business_with_owner(pg_session, 1)
    result = await pg_session.execute(
        text(
            "INSERT INTO pending_actions "
            "(business_id, action_type, payload_schema_version, proposed_payload, "
            " payload_digest, status, expires_at, idempotency_key, version) "
            "VALUES (1, 'appointment', 1, '{}'::jsonb, 'd-appt', 'collecting_details', "
            " now() + interval '1 hour', 'appt-not-cb', 1) RETURNING id"
        )
    )
    appt_id = int(result.scalar_one())
    await pg_session.flush()

    with pytest.raises(PendingActionNotFoundError):
        await CallbackWorklistService(pg_session).resolve(_owner(1), appt_id, expected_version=1)
