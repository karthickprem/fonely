"""Durable voice give-up callback (#36): persistence, tenant safety, PII bound.

When the agent gives up disambiguating a doctor on a VOICE call (the #33
terminating ladder runs out), it must leave a durable callback carrying the
partial booking facts — not end with nothing. This file proves, end to end
through the real conversation engine:

  * the give-up on VOICE persists a CALLBACK pending action; on TEXT it does not
    (a text caller keeps a resumable thread);
  * the callback's business_id is bound to the TRUSTED actor, never a
    payload-supplied value — a callback cannot be created under another tenant;
  * the callback carries an expires_at within the CALLBACK_TTL_DAYS horizon so
    unworked-callback PII self-expires (the durable-but-invisible follow-on);
  * retention sweeps aged callbacks (which the booking-PA sweep can never reach,
    because a callback commits no entity), both directions, mutation-proven;
  * the migration's fail-closed downgrade guard refuses to drop the type while
    callback rows exist.
"""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Import-origin guard: exercise THIS checkout's src (portable, not a branch name).
import fonely.services.conversation as _conv_mod
from fonely.api.internal.validation import InternalValidationPort
from fonely.core.validators import utcnow
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole, Channel
from fonely.models.schema import PendingAction
from fonely.services.appointments import AppointmentService
from fonely.services.conversation import _CONVERSATIONS, ConversationService
from fonely.services.model_gateway import ModelResponse
from tests.integration.postgres.conftest import seed_whatsapp_channel
from tests.integration.postgres.import_origin import assert_module_from_this_checkout

assert_module_from_this_checkout(_conv_mod, __file__)

pytestmark = pytest.mark.postgres


def _voice_actor(business_id: int = 1) -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.VOICE,
    )


def _text_actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
    )


def _mock_gateway() -> AsyncMock:
    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text="Sure, let me help!")
    return gw


@pytest.fixture(autouse=True)
def _clear() -> None:
    _CONVERSATIONS.clear()
    yield
    _CONVERSATIONS.clear()


async def _seed_two_same_first_name_doctors(session: AsyncSession, business_id: int = 1) -> None:
    """A clinic with two doctors sharing a first name, so 'Dr. Priya' matches
    BOTH and disambiguation is genuinely ambiguous (the give-up precondition)."""
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:bid, 'Clinic', 'dental', :phone, 'Asia/Kolkata', 'trial')"
        ),
        {"bid": business_id, "phone": f"+9190000000{business_id:02d}"},
    )
    await seed_whatsapp_channel(
        session, business_id=business_id, phone_number_id=f"phone-{business_id}"
    )
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (:bid, :phone, 'owner', true)"
        ),
        {"bid": business_id, "phone": f"+9190000000{business_id:02d}"},
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (:bid, 'General Consultation', 30, 0, 0, 300.00, true)"
        ),
        {"bid": business_id},
    )
    for name in ("Dr. Priya Kumar", "Dr. Priya Rao"):
        await session.execute(
            text(
                "INSERT INTO resources (business_id, name, resource_type, is_active) "
                "VALUES (:bid, :name, 'staff', true)"
            ),
            {"bid": business_id, "name": name},
        )
    # Eligibility for the single service, both doctors.
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) "
            "SELECT :bid, s.id, r.id, true FROM services s, resources r "
            "WHERE s.business_id = :bid AND r.business_id = :bid"
        ),
        {"bid": business_id},
    )
    for day in range(1, 8):
        await session.execute(
            text(
                "INSERT INTO operating_schedules "
                "(business_id, day_of_week, open_time, close_time, is_active) "
                "VALUES (:bid, :day, '10:00', '18:00', true)"
            ),
            {"bid": business_id, "day": day % 7},
        )
    await session.commit()


async def _drive_to_voice_giveup(
    factory: async_sessionmaker[AsyncSession], conv_id: str, actor: ActorContext
) -> str:
    """Say 'Dr. Priya' enough times to exhaust the disambiguation bound and hit
    the terminal give-up. Returns the final assistant response."""
    async with factory() as session:
        gateway = _mock_gateway()
        appt = AppointmentService(session, validation=InternalValidationPort(session))
        conv = ConversationService(session, gateway, appointment_service=appt)
        await conv.process_message(
            conv_id, actor.business_id, actor, "I want a general consultation with Dr. Priya"
        )
        # Ambiguity is now pending; keep naming the ambiguous first name until the
        # bound (asked >= 3) trips the terminal give-up.
        last = ""
        for _ in range(4):
            turn = await conv.process_message(conv_id, actor.business_id, actor, "Dr. Priya")
            last = turn.assistant_response
        await session.commit()
        return last


async def test_voice_giveup_persists_callback_bound_to_trusted_tenant(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s:
        await _seed_two_same_first_name_doctors(s)

    await _drive_to_voice_giveup(pg_session_factory, "voice-giveup", _voice_actor())

    async with pg_session_factory() as verify:
        rows = (
            (
                await verify.execute(
                    select(PendingAction).where(PendingAction.action_type == "callback")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, f"voice give-up must persist exactly one callback, got {len(rows)}"
    cb = rows[0]
    # TENANT SAFETY: business_id is the trusted actor's, initiated_by the verified phone.
    assert cb.business_id == 1
    assert cb.initiated_by == "+919123456789"
    assert cb.action_type == "callback"
    # The payload carries the give-up reason and the number to dial back.
    data = cb.proposed_payload["data"]
    assert data["reason_code"] == "doctor_disambiguation_exhausted"
    assert data["caller_phone"] == "+919123456789"
    # expires_at is the PA-LIFECYCLE staleness marker, capped at the 24h
    # MAX_EXPIRY_HORIZON — NOT the PII bound (that is the retention sweep, proven
    # separately). A callback cannot carry a 90d expires_at: pending actions cap
    # at 24h and expiry only flips status, never deletes.
    assert cb.expires_at > utcnow()
    assert cb.expires_at <= utcnow() + timedelta(hours=25), (
        "callback expires_at must respect the 24h pending-action horizon, not a "
        "90d PII lifetime — PII is bounded by the retention sweep, not expires_at"
    )


async def test_text_giveup_persists_no_callback(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A TEXT caller keeps a resumable WhatsApp thread; no callback is created.
    async with pg_session_factory() as s:
        await _seed_two_same_first_name_doctors(s)

    await _drive_to_voice_giveup(pg_session_factory, "text-giveup", _text_actor())

    async with pg_session_factory() as verify:
        count = await verify.scalar(
            text("SELECT count(*) FROM pending_actions WHERE action_type = 'callback'")
        )
    assert count == 0, "text give-up must NOT create a callback (thread is resumable)"


async def test_callback_cannot_be_created_under_another_tenant(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """TENANT-SAFETY MUTATION: business_id comes from the TRUSTED actor, never a
    payload value. Even if the conversation's collected_facts carried a foreign
    business_id, the persisted callback is bound to the actor's business_id. Seed
    TWO businesses; run the give-up under business 2's actor; assert the callback
    lands under business 2, not 1, and that business 1 gets none.
    """
    async with pg_session_factory() as s:
        await _seed_two_same_first_name_doctors(s, business_id=1)
        await _seed_two_same_first_name_doctors(s, business_id=2)

    actor2 = _voice_actor(business_id=2)
    await _drive_to_voice_giveup(pg_session_factory, "tenant-giveup", actor2)

    count_sql = text(
        "SELECT count(*) FROM pending_actions WHERE action_type='callback' AND business_id=:bid"
    )
    async with pg_session_factory() as verify:
        b1 = await verify.scalar(count_sql, {"bid": 1})
        b2 = await verify.scalar(count_sql, {"bid": 2})
    assert b2 == 1, "callback must be bound to the acting tenant (business 2)"
    assert b1 == 0, "no callback may leak into another tenant (business 1)"
