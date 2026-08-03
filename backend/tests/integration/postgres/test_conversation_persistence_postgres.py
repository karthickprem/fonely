"""PostgreSQL tests for conversation persistence and restart survival."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.conversation.state import (
    ConversationContext,
    ConversationIntent,
    ConversationState,
    ConversationTurn,
)
from fonely.repositories.conversations import ConversationRepository
from fonely.services.conversation import _CONVERSATIONS
from fonely.services.conversation_persistence import ConversationPersistenceService

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


async def _seed_business(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental', 'clinic', '+914428350001', "
            "'Asia/Kolkata', 'trial')"
        )
    )


def _make_turn(
    ctx: ConversationContext, turn_num: int, message: str, response: str
) -> ConversationTurn:
    return ConversationTurn(
        turn_id=f"turn-{turn_num}",
        conversation_id=ctx.conversation_id,
        business_id=ctx.business_id,
        state=ctx.state,
        user_message=message,
        assistant_response=response,
        collected_facts=dict(ctx.collected_facts),
        missing_facts=["service_id", "start_at"],
        intent=ConversationIntent.BOOK_APPOINTMENT,
        safety_classification="administrative",
    )


async def test_full_persistence_lifecycle(pg_session: AsyncSession) -> None:
    """Functional proof A: 4 turns persisted with no user message text."""
    await _seed_business(pg_session)
    persistence = ConversationPersistenceService(pg_session)

    ctx = await persistence.load_or_create(1, "+919123456789")
    assert ctx.state == ConversationState.GREETING
    conv_id = ctx.conversation_id

    for i in range(1, 5):
        ctx.collected_facts[f"fact_{i}"] = f"value_{i}"
        ctx.state = ConversationState.FACT_COLLECTION
        turn = _make_turn(ctx, i, f"user message {i}", f"response {i}")
        ctx.turns.append(turn)
        await persistence.save_turn(ctx, turn)

    conv = (
        await pg_session.execute(
            text("SELECT id, state, turn_count, collected_facts FROM conversations WHERE id = :id"),
            {"id": conv_id},
        )
    ).one()
    assert conv[1] == "fact_collection"
    assert conv[2] == 4
    assert conv[3]["fact_1"] == "value_1"
    assert conv[3]["fact_4"] == "value_4"

    turns = (
        await pg_session.execute(
            text(
                "SELECT turn_number, user_message_hash, assistant_response "
                "FROM conversation_turns WHERE conversation_id = :cid ORDER BY turn_number"
            ),
            {"cid": conv_id},
        )
    ).all()
    assert len(turns) == 4
    for _t_num, msg_hash, response in turns:
        assert len(msg_hash) == 64
        assert "user message" not in msg_hash
        assert response.startswith("response")

    no_text = await pg_session.scalar(
        text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'conversation_turns' AND column_name = 'user_message'"
        )
    )
    assert no_text == 0

    await persistence.mark_completed(conv_id)
    completed = (
        await pg_session.execute(
            text("SELECT state, completed_at FROM conversations WHERE id = :id"),
            {"id": conv_id},
        )
    ).one()
    assert completed[0] == "completed"
    assert completed[1] is not None


async def test_restart_simulation(pg_session: AsyncSession) -> None:
    """Functional proof B: conversation survives cache clear."""
    await _seed_business(pg_session)
    persistence = ConversationPersistenceService(pg_session)

    ctx = await persistence.load_or_create(1, "+919111111111")
    conv_id = ctx.conversation_id

    ctx.collected_facts["service_id"] = 1
    ctx.collected_facts["service_name"] = "Scaling"
    ctx.state = ConversationState.FACT_COLLECTION
    turn1 = _make_turn(ctx, 1, "scaling appointment", "Which day?")
    ctx.turns.append(turn1)
    await persistence.save_turn(ctx, turn1)

    ctx.collected_facts["date"] = "tomorrow"
    turn2 = _make_turn(ctx, 2, "tomorrow", "What time?")
    ctx.turns.append(turn2)
    await persistence.save_turn(ctx, turn2)

    db_turns = await pg_session.scalar(
        text("SELECT count(*) FROM conversation_turns WHERE conversation_id = :cid"),
        {"cid": conv_id},
    )
    assert db_turns == 2

    _CONVERSATIONS.clear()
    assert conv_id not in _CONVERSATIONS

    reloaded = await persistence.load_by_id(conv_id)
    assert reloaded is not None
    assert reloaded.conversation_id == conv_id
    assert reloaded.collected_facts.get("service_id") == 1
    assert reloaded.collected_facts.get("service_name") == "Scaling"
    assert reloaded.collected_facts.get("date") == "tomorrow"
    assert reloaded.state == ConversationState.FACT_COLLECTION


async def test_phone_continuity(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session)
    await pg_session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (2, 'Other Clinic', 'clinic', '+919999999999', 'Asia/Kolkata', 'trial')"
        )
    )
    persistence = ConversationPersistenceService(pg_session)

    ctx1 = await persistence.load_or_create(1, "+919123456789")
    ctx2 = await persistence.load_or_create(1, "+919123456789")
    assert ctx1.conversation_id == ctx2.conversation_id

    ctx3 = await persistence.load_or_create(2, "+919123456789")
    assert ctx3.conversation_id != ctx1.conversation_id

    await persistence.mark_completed(ctx1.conversation_id)
    ctx4 = await persistence.load_or_create(1, "+919123456789")
    assert ctx4.conversation_id != ctx1.conversation_id


async def test_cleanup_expired(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session)
    repo = ConversationRepository(pg_session)

    await repo.create(
        {
            "id": "expired-conv",
            "business_id": 1,
            "customer_phone": "+919000000001",
            "state": "fact_collection",
            "collected_facts": {},
            "turn_count": 0,
            "expires_at": datetime.now(UTC) - timedelta(hours=2),
        }
    )
    await repo.create(
        {
            "id": "active-conv",
            "business_id": 1,
            "customer_phone": "+919000000002",
            "state": "greeting",
            "collected_facts": {},
            "turn_count": 0,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
    )

    persistence = ConversationPersistenceService(pg_session)
    count = await persistence.cleanup_expired()
    assert count == 1

    active = await repo.get_by_id("active-conv")
    assert active is not None
    expired = await repo.get_by_id("expired-conv")
    assert expired is None
