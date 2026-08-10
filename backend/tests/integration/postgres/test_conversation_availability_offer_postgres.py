"""PostgreSQL evidence for durable availability-offer conversation state."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.domain.conversation.state import (
    ConversationState,
    ConversationTurn,
)
from fonely.services.availability import AvailableSlot
from fonely.services.availability_offers import create_availability_offer
from fonely.services.conversation_persistence import ConversationPersistenceService

pytestmark = pytest.mark.postgres


def _offer(conversation_id: str, *, business_id: int = 1):
    start = datetime(2099, 8, 10, 11, 30, tzinfo=UTC)
    return create_availability_offer(
        business_id=business_id,
        conversation_id=conversation_id,
        service_id=1,
        business_timezone="Asia/Kolkata",
        alternatives=(
            AvailableSlot(
                start_at=start,
                end_at=start + timedelta(minutes=30),
                resource_id=1,
                resource_name="Dr. Priya",
            ),
        ),
        now=datetime(2099, 8, 10, 4, 0, tzinfo=UTC),
    )


async def _seed_business(session: AsyncSession, business_id: int = 1) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:id, :name, 'dental', :phone, 'Asia/Kolkata', 'trial')"
        ),
        {
            "id": business_id,
            "name": f"Business {business_id}",
            "phone": f"+91900000000{business_id}",
        },
    )
    await session.commit()


async def test_offer_and_selected_ref_survive_fresh_session(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_business(session)
        persistence = ConversationPersistenceService(session)
        ctx = await persistence.load_or_create(1, "+919123456789")
        ctx.state = ConversationState.FACT_COLLECTION
        ctx.collected_facts = {
            "service_id": 1,
            "start_at": datetime(2099, 8, 10, 10, 0, tzinfo=UTC),
        }
        ctx.availability_offer = _offer(ctx.conversation_id)
        ctx.selected_slot_ref = ctx.availability_offer.slots[0]
        turn = ConversationTurn(
            turn_id="turn-1",
            conversation_id=ctx.conversation_id,
            business_id=1,
            state=ctx.state,
            user_message="5 PM",
            assistant_response="Selected 5 PM",
            collected_facts=dict(ctx.collected_facts),
            missing_facts=[],
        )
        ctx.turns.append(turn)
        await persistence.save_turn(ctx, turn)
        await session.commit()
        conversation_id = ctx.conversation_id
        original_offer = ctx.availability_offer

    async with pg_session_factory() as fresh_session:
        restored = await ConversationPersistenceService(fresh_session).load_by_id(conversation_id)

    assert restored is not None
    assert restored.availability_offer == original_offer
    assert restored.selected_slot_ref == original_offer.slots[0]
    assert restored.collected_facts["start_at"] == datetime(2099, 8, 10, 10, 0, tzinfo=UTC)


async def test_tampered_persisted_offer_is_not_restored(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_business(session)
        persistence = ConversationPersistenceService(session)
        ctx = await persistence.load_or_create(1, "+919123456789")
        ctx.availability_offer = _offer(ctx.conversation_id)
        turn = ConversationTurn(
            turn_id="turn-1",
            conversation_id=ctx.conversation_id,
            business_id=1,
            state=ctx.state,
            user_message="test",
            assistant_response="test",
            collected_facts={},
            missing_facts=[],
        )
        ctx.turns.append(turn)
        await persistence.save_turn(ctx, turn)
        await session.commit()
        conversation_id = ctx.conversation_id

        await session.execute(
            text(
                "UPDATE conversations "
                "SET collected_facts = jsonb_set(collected_facts, "
                "'{_availability_offer,business_id}', '2'::jsonb) "
                "WHERE id = :id"
            ),
            {"id": conversation_id},
        )
        await session.commit()

    async with pg_session_factory() as fresh_session:
        restored = await ConversationPersistenceService(fresh_session).load_by_id(conversation_id)

    assert restored is not None
    assert restored.availability_offer is None
