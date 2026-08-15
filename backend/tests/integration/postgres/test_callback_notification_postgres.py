"""Owner callback notification (#41 part A): emit + tenant-scope + persist-then-notify.

#41-B made callbacks queryable/resolvable by the owner. A actively PUSHES an
owner WhatsApp notification when a voice give-up persists a callback, so the
owner is TOLD, not left to poll. This proves:
  * create_callback_notification emits exactly one OWNER outbox event with the
    callback_requested event type, entity_type='pending_action' (NOT appointment),
    carrying the partial facts;
  * it is tenant-scoped (owner of business A is never notified for B's callback)
    and idempotent (re-emit for the same callback → one row);
  * the voice give-up emits BOTH the callback pending action AND the owner
    notification end-to-end;
  * persist-then-notify degrades gracefully: when the owner push cannot be built
    (no WhatsApp channel), the callback ROW still persists — "queryable but not
    pushed" (B's guarantee), never a lost follow-up.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Import-origin guard: exercise THIS checkout's src.
import fonely.services.notifications as _notif_mod
from fonely.services.notifications import NotificationService
from tests.integration.postgres.conftest import seed_whatsapp_channel
from tests.integration.postgres.import_origin import assert_module_from_this_checkout
from tests.integration.postgres.test_voice_callback_postgres import (
    _drive_to_voice_giveup,
    _seed_two_same_first_name_doctors,
    _voice_actor,
)

assert_module_from_this_checkout(_notif_mod, __file__)

pytestmark = pytest.mark.postgres


async def _seed_business_with_owner_and_channel(session: AsyncSession, business_id: int) -> str:
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
    await seed_whatsapp_channel(
        session, business_id=business_id, phone_number_id=f"phone-{business_id}"
    )
    await session.commit()
    return owner_phone


async def _insert_callback_pa(session: AsyncSession, business_id: int, key: str) -> int:
    result = await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(business_id, action_type, payload_schema_version, proposed_payload, "
            " payload_digest, status, expires_at, idempotency_key, initiated_by, version) "
            "VALUES (:bid, 'callback', 1, '{}'::jsonb, :digest, 'collecting_details', "
            " now() + interval '1 hour', :key, '+919123456789', 1) RETURNING id"
        ),
        {"bid": business_id, "digest": f"d-{key}", "key": key},
    )
    await session.flush()
    return int(result.scalar_one())


async def test_emit_creates_one_owner_event_with_callback_facts(
    pg_session: AsyncSession,
) -> None:
    await _seed_business_with_owner_and_channel(pg_session, 1)
    pa_id = await _insert_callback_pa(pg_session, 1, "cb-emit")

    ids = await NotificationService(pg_session).create_callback_notification(
        business_id=1,
        callback_pending_action_id=pa_id,
        caller_phone="+919123456789",
        reason_code="doctor_disambiguation_exhausted",
        service_name="General Consultation",
        target_date="2026-08-20",
        attempted_candidates=["Dr. Priya Kumar", "Dr. Priya Rao"],
    )
    assert len(ids) == 1, "exactly one owner notification for a single-owner business"

    row = (
        await pg_session.execute(
            text(
                "SELECT event_type, entity_type, entity_id, recipient_type, "
                "recipient_phone, payload FROM notification_outbox WHERE id = :id"
            ),
            {"id": ids[0]},
        )
    ).one()
    assert row[0] == "callback_requested"
    assert row[1] == "pending_action", "a callback references a pending_action, NOT an appointment"
    assert row[2] == pa_id
    assert row[3] == "owner"
    assert row[4] == "+919000000001"
    payload = row[5]
    assert payload["caller_phone"] == "+919123456789"
    assert payload["service_name"] == "General Consultation"
    assert payload["target_date"] == "2026-08-20"
    assert payload["attempted_candidates"] == ["Dr. Priya Kumar", "Dr. Priya Rao"]


async def test_emit_is_idempotent_per_callback(pg_session: AsyncSession) -> None:
    await _seed_business_with_owner_and_channel(pg_session, 1)
    pa_id = await _insert_callback_pa(pg_session, 1, "cb-idem")
    svc = NotificationService(pg_session)

    first = await svc.create_callback_notification(
        business_id=1,
        callback_pending_action_id=pa_id,
        caller_phone="+919123456789",
        reason_code="doctor_disambiguation_exhausted",
    )
    assert len(first) == 1
    # Re-emit for the SAME callback: on-conflict-do-nothing → no new row.
    second = await svc.create_callback_notification(
        business_id=1,
        callback_pending_action_id=pa_id,
        caller_phone="+919123456789",
        reason_code="doctor_disambiguation_exhausted",
    )
    assert second == [], "re-emit for the same callback must not duplicate the owner event"

    count = await pg_session.scalar(
        text(
            "SELECT count(*) FROM notification_outbox "
            "WHERE event_type = 'callback_requested' AND entity_id = :id"
        ),
        {"id": pa_id},
    )
    assert count == 1


async def test_emit_is_tenant_scoped(pg_session: AsyncSession) -> None:
    # Business 1's callback must notify business 1's owner only — never business 2.
    await _seed_business_with_owner_and_channel(pg_session, 1)
    await _seed_business_with_owner_and_channel(pg_session, 2)
    pa_id = await _insert_callback_pa(pg_session, 1, "cb-tenant")

    ids = await NotificationService(pg_session).create_callback_notification(
        business_id=1,
        callback_pending_action_id=pa_id,
        caller_phone="+919123456789",
        reason_code="doctor_disambiguation_exhausted",
    )
    assert len(ids) == 1
    recipient = await pg_session.scalar(
        text("SELECT recipient_phone FROM notification_outbox WHERE id = :id"), {"id": ids[0]}
    )
    assert recipient == "+919000000001", "must notify business 1's owner"

    # Business 2 got nothing.
    b2 = await pg_session.scalar(
        text(
            "SELECT count(*) FROM notification_outbox "
            "WHERE event_type = 'callback_requested' AND business_id = 2"
        )
    )
    assert b2 == 0


async def test_voice_giveup_emits_callback_and_owner_notification_end_to_end(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s:
        await _seed_two_same_first_name_doctors(s)

    await _drive_to_voice_giveup(pg_session_factory, "voice-notify", _voice_actor())

    async with pg_session_factory() as verify:
        # The callback pending action was persisted (#36) ...
        cb_count = await verify.scalar(
            text("SELECT count(*) FROM pending_actions WHERE action_type = 'callback'")
        )
        assert cb_count == 1
        # ... AND an owner callback_requested notification was emitted (#41-A).
        notif = (
            await verify.execute(
                text(
                    "SELECT recipient_type, event_type, entity_type, payload "
                    "FROM notification_outbox WHERE event_type = 'callback_requested'"
                )
            )
        ).one_or_none()
        assert notif is not None, "voice give-up must emit an owner callback notification"
        assert notif[0] == "owner"
        assert notif[1] == "callback_requested"
        assert notif[2] == "pending_action"
        assert notif[3]["caller_phone"] == "+919123456789"


async def test_notify_failure_leaves_callback_persisted(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PERSIST-THEN-NOTIFY degradation: seed a business WITHOUT a WhatsApp channel,
    so create_callback_notification raises whatsapp_mapping_missing. The give-up
    must still leave the callback ROW persisted (owner can PULL it via #41-B) —
    the push is dropped and logged, not the durable record.
    """
    async with pg_session_factory() as s:
        # Two same-name doctors + owner, but NO seed_whatsapp_channel — so the
        # notification's channel-context resolution fails.
        await s.execute(
            text(
                "INSERT INTO businesses "
                "(id, name, category, primary_contact_phone, timezone, subscription) "
                "VALUES (1, 'Clinic', 'dental', '+919000000001', 'Asia/Kolkata', 'trial')"
            )
        )
        await s.execute(
            text(
                "INSERT INTO business_users (business_id, phone, role, is_active) "
                "VALUES (1, '+919000000001', 'owner', true)"
            )
        )
        await s.execute(
            text(
                "INSERT INTO services "
                "(business_id, name, duration_minutes, buffer_before_minutes, "
                "buffer_after_minutes, price, is_active) "
                "VALUES (1, 'General Consultation', 30, 0, 0, 300.00, true)"
            )
        )
        for name in ("Dr. Priya Kumar", "Dr. Priya Rao"):
            await s.execute(
                text(
                    "INSERT INTO resources (business_id, name, resource_type, is_active) "
                    "VALUES (1, :name, 'staff', true)"
                ),
                {"name": name},
            )
        await s.execute(
            text(
                "INSERT INTO service_resource_eligibility "
                "(business_id, service_id, resource_id, is_active) "
                "SELECT 1, sv.id, r.id, true FROM services sv, resources r "
                "WHERE sv.business_id = 1 AND r.business_id = 1"
            )
        )
        for day in range(7):
            await s.execute(
                text(
                    "INSERT INTO operating_schedules "
                    "(business_id, day_of_week, open_time, close_time, is_active) "
                    "VALUES (1, :day, '10:00', '18:00', true)"
                ),
                {"day": day},
            )
        await s.commit()

    await _drive_to_voice_giveup(pg_session_factory, "notify-degrade", _voice_actor())

    async with pg_session_factory() as verify:
        # The callback survived even though the push could not be built.
        cb = await verify.scalar(
            text("SELECT count(*) FROM pending_actions WHERE action_type = 'callback'")
        )
        assert cb == 1, "the callback ROW must persist even when the owner push fails"
        # No notification row (the emit failed closed, logged, did not partially write).
        notif = await verify.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE event_type = 'callback_requested'")
        )
        assert notif == 0, "a failed push must not leave a partial notification row"
