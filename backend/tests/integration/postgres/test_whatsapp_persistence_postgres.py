"""PostgreSQL integration tests for WhatsApp conversation persistence.

Proves conversations survive server restarts by persisting to PostgreSQL
via find_or_create_conversation_persistent. Simulates restart by clearing
the in-memory _CONVERSATIONS cache between messages.
"""

from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fonely.api.internal.validation import InternalValidationPort
from fonely.core.validators import utcnow
from fonely.domain.conversation.state import ConversationState
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.models.schema import Conversation
from fonely.services.appointments import AppointmentService
from fonely.services.conversation import (
    _CONVERSATIONS,
    _PHONE_INDEX,
    ConversationService,
    find_or_create_conversation_persistent,
)
from fonely.services.model_gateway import ModelResponse

pytestmark = pytest.mark.postgres


def _actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
    )


def _mock_gateway() -> AsyncMock:
    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text="Sure!")
    return gw


async def _seed_clinic(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental', 'dental', '+919000000001', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'Consultation', 30, 0, 0, 300.00, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Dr. Priya', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) "
            "VALUES (1, 1, 1, true)"
        )
    )
    for day in range(1, 7):
        await session.execute(
            text(
                "INSERT INTO operating_schedules "
                "(business_id, day_of_week, open_time, close_time, is_active) "
                "VALUES (1, :day, '10:00', '13:00', true)"
            ),
            {"day": day},
        )
        await session.execute(
            text(
                "INSERT INTO operating_schedules "
                "(business_id, day_of_week, open_time, close_time, is_active) "
                "VALUES (1, :day, '17:00', '20:30', true)"
            ),
            {"day": day},
        )
    await session.commit()


@pytest.fixture(autouse=True)
def _clear_caches():
    _CONVERSATIONS.clear()
    _PHONE_INDEX.clear()
    yield
    _CONVERSATIONS.clear()
    _PHONE_INDEX.clear()


async def test_conversation_persists_across_restart(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)

    async with pg_session_factory() as session:
        ctx1 = await find_or_create_conversation_persistent(1, "+919123456789", session)
        conv_id = ctx1.conversation_id
        await session.commit()

    db_row = None
    async with pg_session_factory() as session:
        db_row = (
            await session.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one_or_none()
    assert db_row is not None, "Conversation not persisted to DB"

    async with pg_session_factory() as session:
        gateway = _mock_gateway()
        validation = InternalValidationPort(session)
        appt_service = AppointmentService(session, validation=validation)
        conv_service = ConversationService(session, gateway, appointment_service=appt_service)
        turn1 = await conv_service.process_message(conv_id, 1, _actor(), "Book appointment")
        await session.commit()
        assert turn1.state == ConversationState.FACT_COLLECTION

    _CONVERSATIONS.clear()
    _PHONE_INDEX.clear()

    async with pg_session_factory() as session:
        ctx_reloaded = await find_or_create_conversation_persistent(1, "+919123456789", session)
        assert ctx_reloaded.conversation_id == conv_id
        assert ctx_reloaded.state == ConversationState.FACT_COLLECTION


async def test_completed_conversation_starts_new(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)

    now = utcnow()
    target = now + timedelta(days=1)
    if target.isoweekday() == 7:
        target += timedelta(days=1)
    slot_start = datetime.combine(target.date(), time(10, 30), tzinfo=UTC)

    async with pg_session_factory() as session:
        ctx = await find_or_create_conversation_persistent(1, "+919123456789", session)
        first_conv_id = ctx.conversation_id

        gateway = _mock_gateway()
        validation = InternalValidationPort(session)
        appt_service = AppointmentService(session, validation=validation)
        conv_service = ConversationService(session, gateway, appointment_service=appt_service)

        await conv_service.process_message(first_conv_id, 1, _actor(), "Book appointment")
        ctx.collected_facts["service_id"] = 1
        ctx.collected_facts["service_name"] = "Consultation"
        ctx.collected_facts["resource_id"] = 1
        ctx.collected_facts["resource_name"] = "Dr. Priya"
        ctx.collected_facts["customer_phone"] = "+919123456789"
        ctx.collected_facts["start_at"] = slot_start

        await conv_service.process_message(first_conv_id, 1, _actor(), "Check slots")
        assert ctx.proposal_id is not None

        turn = await conv_service.process_message(first_conv_id, 1, _actor(), "yes")
        assert turn.state == ConversationState.COMPLETED

        from fonely.services.conversation_persistence import (
            ConversationPersistenceService,
        )

        persistence = ConversationPersistenceService(session)
        await persistence.mark_completed(first_conv_id)
        await session.commit()

    _CONVERSATIONS.clear()
    _PHONE_INDEX.clear()

    async with pg_session_factory() as session:
        ctx_new = await find_or_create_conversation_persistent(1, "+919123456789", session)
        assert ctx_new.conversation_id != first_conv_id
        assert ctx_new.state == ConversationState.GREETING
