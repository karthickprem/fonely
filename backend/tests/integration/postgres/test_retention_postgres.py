"""PostgreSQL integration tests for data retention cleanup.

Proves that raw SQL in DataRetentionService correctly deletes old
terminal records, preserves active/recent records, respects FK
references, and honours batch limits — against real PostgreSQL.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.validators import utcnow
from fonely.models.schema import (
    Conversation,
    DBConversationTurn,
    NotificationOutboxEvent,
    PendingAction,
)
from fonely.services.data_retention import DataRetentionService

pytestmark = pytest.mark.postgres

_SEED_SQL = (
    "INSERT INTO businesses (id, name, category, primary_contact_phone, timezone) "
    "VALUES (1, 'Smile Dental', 'dental_clinic', '+910000000001', 'Asia/Kolkata')"
)


async def _seed_business(session: AsyncSession) -> None:
    await session.execute(text(_SEED_SQL))
    await session.flush()


def _conv_id() -> str:
    return str(uuid.uuid4())


async def _insert_conversation(
    session: AsyncSession,
    *,
    state: str = "completed",
    updated_at_offset_days: int = 0,
    with_turns: int = 0,
) -> str:
    conv_id = _conv_id()
    now = utcnow()
    updated = now - timedelta(days=updated_at_offset_days)
    await session.execute(
        text(
            "INSERT INTO conversations "
            "(id, business_id, customer_phone, state, collected_facts, "
            " turn_count, expires_at, created_at, updated_at) "
            "VALUES (:id, 1, '+919123456789', :state, '{}'::jsonb, "
            " :turns, :expires, :created, :updated)"
        ),
        {
            "id": conv_id,
            "state": state,
            "turns": with_turns,
            "expires": now + timedelta(hours=1),
            "created": updated,
            "updated": updated,
        },
    )
    for i in range(with_turns):
        turn_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO conversation_turns "
                "(id, conversation_id, business_id, turn_number, state, "
                " intent, safety_classification, user_message_hash, "
                " assistant_response, collected_facts_snapshot, missing_facts, created_at) "
                "VALUES (:id, :conv, 1, :num, 'greeting', 'booking', 'safe', "
                " :hash, 'response', '{}'::jsonb, '[]'::jsonb, :ts)"
            ),
            {
                "id": turn_id,
                "conv": conv_id,
                "num": i + 1,
                "hash": f"hash_{conv_id}_{i}",
                "ts": updated,
            },
        )
    await session.flush()
    return conv_id


async def _insert_notification(
    session: AsyncSession,
    *,
    status: str = "delivered",
    updated_at_offset_days: int = 0,
    idempotency_suffix: str = "",
) -> int:
    now = utcnow()
    updated = now - timedelta(days=updated_at_offset_days)
    result = await session.execute(
        text(
            "INSERT INTO notification_outbox "
            "(business_id, event_type, entity_type, entity_id, "
            " recipient_type, recipient_phone, channel, payload, "
            " status, idempotency_key, created_at, updated_at) "
            "VALUES (1, 'appointment_confirmed', 'appointment', 1, "
            " 'patient', '+919123456789', 'whatsapp', '{}'::jsonb, "
            " :status, :key, :created, :updated) "
            "RETURNING id"
        ),
        {
            "status": status,
            "key": f"test-notif-{uuid.uuid4()}{idempotency_suffix}",
            "created": updated,
            "updated": updated,
        },
    )
    row = result.one()
    await session.flush()
    return row[0]


class TestConversationCleanup:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_cleanup_old_completed_conversations(self, pg_session: AsyncSession) -> None:
        await _seed_business(pg_session)

        old_completed = await _insert_conversation(
            pg_session, state="completed", updated_at_offset_days=100, with_turns=3
        )
        recent_completed = await _insert_conversation(
            pg_session, state="completed", updated_at_offset_days=10, with_turns=2
        )
        old_active = await _insert_conversation(
            pg_session, state="greeting", updated_at_offset_days=100, with_turns=1
        )

        svc = DataRetentionService(pg_session)
        result = await svc.run_cleanup()

        assert result.conversations_deleted == 1
        assert result.turns_deleted == 3

        remaining = (await pg_session.execute(select(Conversation.id))).scalars().all()
        assert old_completed not in remaining
        assert recent_completed in remaining
        assert old_active in remaining

        old_turns = (
            await pg_session.execute(
                select(func.count())
                .select_from(DBConversationTurn)
                .where(DBConversationTurn.conversation_id == old_completed)
            )
        ).scalar()
        assert old_turns == 0

        recent_turns = (
            await pg_session.execute(
                select(func.count())
                .select_from(DBConversationTurn)
                .where(DBConversationTurn.conversation_id == recent_completed)
            )
        ).scalar()
        assert recent_turns == 2

        active_turns = (
            await pg_session.execute(
                select(func.count())
                .select_from(DBConversationTurn)
                .where(DBConversationTurn.conversation_id == old_active)
            )
        ).scalar()
        assert active_turns == 1


class TestNotificationCleanup:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_cleanup_old_delivered_notifications(self, pg_session: AsyncSession) -> None:
        await _seed_business(pg_session)

        old_delivered_id = await _insert_notification(
            pg_session, status="delivered", updated_at_offset_days=45
        )
        recent_delivered_id = await _insert_notification(
            pg_session, status="delivered", updated_at_offset_days=15
        )
        pending_id = await _insert_notification(
            pg_session, status="pending", updated_at_offset_days=100
        )

        svc = DataRetentionService(pg_session)
        result = await svc.run_cleanup()

        assert result.notifications_deleted >= 1

        remaining_ids = (
            (await pg_session.execute(select(NotificationOutboxEvent.id))).scalars().all()
        )
        assert old_delivered_id not in remaining_ids
        assert recent_delivered_id in remaining_ids
        assert pending_id in remaining_ids


class TestPendingActionProtection:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_cleanup_protects_referenced_pending_actions(
        self, pg_session: AsyncSession
    ) -> None:
        await _seed_business(pg_session)

        now = utcnow()
        old = now - timedelta(days=100)

        await pg_session.execute(
            text(
                "INSERT INTO services (id, business_id, name, duration_minutes, "
                " buffer_before_minutes, buffer_after_minutes, price, is_active) "
                "VALUES (1, 1, 'Consultation', 30, 0, 0, 500, true)"
            )
        )
        await pg_session.execute(
            text(
                "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
                "VALUES (1, 1, 'Dr. Priya', 'dentist', true)"
            )
        )
        await pg_session.flush()

        pa_result = await pg_session.execute(
            text(
                "INSERT INTO pending_actions "
                "(business_id, action_type, status, proposed_payload, "
                " committed_entity_type, committed_entity_id, "
                " created_at, updated_at, version) "
                "VALUES (1, 'appointment', 'confirmed', '{}'::jsonb, "
                " 'appointment', 1, :old, :old, 1) "
                "RETURNING id"
            ),
            {"old": old},
        )
        pa_id = pa_result.scalar_one()

        start = now + timedelta(hours=2)
        end = start + timedelta(minutes=30)
        await pg_session.execute(
            text(
                "INSERT INTO appointments "
                "(business_id, resource_id, service_id, start_at, end_at, "
                " effective_start_at, effective_end_at, status, source, "
                " service_name_snapshot, resource_name_snapshot, "
                " duration_minutes_snapshot, business_timezone_snapshot, "
                " pending_action_id, idempotency_key) "
                "VALUES (1, 1, 1, :start, :end, :start, :end, 'confirmed', "
                " 'customer_conversation', 'Consultation', 'Dr. Priya', "
                " 30, 'Asia/Kolkata', :pa_id, :idem)"
            ),
            {"start": start, "end": end, "pa_id": pa_id, "idem": f"pa-{pa_id}"},
        )
        await pg_session.flush()

        svc = DataRetentionService(pg_session)
        result = await svc.run_cleanup()

        assert result.pending_actions_deleted == 0

        remaining = await pg_session.scalar(
            select(func.count()).select_from(PendingAction).where(PendingAction.id == pa_id)
        )
        assert remaining == 1


class TestBatchSize:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_batch_size_respected(self, pg_session: AsyncSession) -> None:
        await _seed_business(pg_session)

        for _i in range(1500):
            await _insert_conversation(pg_session, state="completed", updated_at_offset_days=100)
        await pg_session.flush()

        svc = DataRetentionService(pg_session)

        result1 = await svc.run_cleanup()
        assert result1.conversations_deleted == 1000

        count_after_first = await pg_session.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.state == "completed")
        )
        assert count_after_first == 500

        result2 = await svc.run_cleanup()
        assert result2.conversations_deleted == 500

        count_after_second = await pg_session.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.state == "completed")
        )
        assert count_after_second == 0
