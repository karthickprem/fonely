"""PostgreSQL integration tests for conversation cancel and reschedule flows."""

from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fonely.api.internal.validation import InternalValidationPort
from fonely.core.validators import utcnow
from fonely.domain.conversation.state import ConversationState
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole, Channel
from fonely.models.schema import Appointment, ResourceAllocation
from fonely.services.appointments import AppointmentService
from fonely.services.conversation import _CONVERSATIONS, ConversationService
from fonely.services.model_gateway import ModelResponse

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import notifications, whatsapp_config

    mappings = '{"phone-1": 1}'
    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_phone_number_id", "phone-1")


def _actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
    )


def _mock_gateway() -> AsyncMock:
    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text="Sure!")
    return gw


async def _seed_dental_clinic(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental Clinic', 'dental', '+919000000001', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (1, '+919000000001', 'owner', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'General Consultation', 30, 0, 0, 300.00, true)"
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


def _next_weekday_slot(hour: int = 10, minute: int = 30) -> datetime:
    now = utcnow()
    target = now + timedelta(days=1)
    if target.isoweekday() == 7:
        target += timedelta(days=1)
    clinic_tz = ZoneInfo("Asia/Kolkata")
    return datetime.combine(target.date(), time(hour, minute), tzinfo=clinic_tz).astimezone(UTC)


async def _book_appointment(
    session: AsyncSession,
    gateway: AsyncMock,
    slot_start: datetime,
) -> int:
    validation = InternalValidationPort(session)
    appt_service = AppointmentService(session, validation=validation)
    conv_service = ConversationService(session, gateway, appointment_service=appt_service)

    await conv_service.process_message("book-for-cancel", 1, _actor(), "Book appointment")
    ctx = _CONVERSATIONS["book-for-cancel"]
    ctx.collected_facts["service_id"] = 1
    ctx.collected_facts["service_name"] = "General Consultation"
    ctx.collected_facts["resource_id"] = 1
    ctx.collected_facts["resource_name"] = "Dr. Priya"
    ctx.collected_facts["customer_phone"] = "+919123456789"
    ctx.collected_facts["start_at"] = slot_start

    await conv_service.process_message("book-for-cancel", 1, _actor(), "Check slots")
    assert ctx.proposal_id is not None

    turn = await conv_service.process_message("book-for-cancel", 1, _actor(), "yes")
    assert turn.state == ConversationState.COMPLETED

    appt = (
        await session.execute(
            select(Appointment).where(
                Appointment.business_id == 1,
                Appointment.status == "confirmed",
            )
        )
    ).scalar_one()
    return appt.id


@pytest.fixture(autouse=True)
def clear_conversations():
    _CONVERSATIONS.clear()
    yield
    _CONVERSATIONS.clear()


async def test_cancel_flow_produces_cancelled_appointment(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)

    slot = _next_weekday_slot()

    async with pg_session_factory() as session:
        gateway = _mock_gateway()
        appt_id = await _book_appointment(session, gateway, slot)
        await session.commit()

    async with pg_session_factory() as session:
        gateway = _mock_gateway()
        validation = InternalValidationPort(session)
        appt_service = AppointmentService(session, validation=validation)
        conv_service = ConversationService(session, gateway, appointment_service=appt_service)

        turn1 = await conv_service.process_message(
            "cancel-flow", 1, _actor(), "cancel my appointment"
        )
        assert turn1.state in (
            ConversationState.CANCEL_SELECTION,
            ConversationState.AWAITING_CONFIRMATION,
        )
        assert "cancel" in turn1.assistant_response.lower()

        turn2 = await conv_service.process_message("cancel-flow", 1, _actor(), "yes")
        assert turn2.state == ConversationState.COMPLETED
        assert "cancelled" in turn2.assistant_response.lower()

        appt = (
            await session.execute(select(Appointment).where(Appointment.id == appt_id))
        ).scalar_one()
        assert appt.status == "cancelled"
        assert appt.cancelled_at is not None


async def test_cancel_no_appointments(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)

    async with pg_session_factory() as session:
        gateway = _mock_gateway()
        validation = InternalValidationPort(session)
        appt_service = AppointmentService(session, validation=validation)
        conv_service = ConversationService(session, gateway, appointment_service=appt_service)

        turn = await conv_service.process_message(
            "cancel-none", 1, _actor(), "cancel my appointment"
        )
        assert "upcoming appointments" in turn.assistant_response.lower()
        assert turn.state == ConversationState.ENDED

        count = await session.scalar(select(func.count(Appointment.id)))
        assert count == 0


async def test_reschedule_flow_moves_appointment(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)

    slot = _next_weekday_slot(10, 30)

    async with pg_session_factory() as session:
        gateway = _mock_gateway()
        appt_id = await _book_appointment(session, gateway, slot)
        await session.commit()

    new_slot = _next_weekday_slot(17, 30)

    async with pg_session_factory() as session:
        gateway = _mock_gateway()
        validation = InternalValidationPort(session)
        appt_service = AppointmentService(session, validation=validation)
        conv_service = ConversationService(session, gateway, appointment_service=appt_service)

        turn1 = await conv_service.process_message(
            "reschedule-flow", 1, _actor(), "reschedule my appointment"
        )
        assert turn1.state in (
            ConversationState.RESCHEDULE_SELECTION,
            ConversationState.FACT_COLLECTION,
        )

        ctx = _CONVERSATIONS["reschedule-flow"]
        ctx.collected_facts["start_at"] = new_slot

        turn2 = await conv_service.process_message(
            "reschedule-flow", 1, _actor(), "5:30 PM tomorrow"
        )
        assert ctx.proposal_id is not None
        assert turn2.state == ConversationState.AWAITING_CONFIRMATION

        turn3 = await conv_service.process_message("reschedule-flow", 1, _actor(), "yes")
        assert turn3.state == ConversationState.COMPLETED
        assert "rescheduled" in turn3.assistant_response.lower()

        appt = (
            await session.execute(select(Appointment).where(Appointment.id == appt_id))
        ).scalar_one()
        assert appt.status == "confirmed"
        assert appt.rescheduled_at is not None

        allocs = (
            (
                await session.execute(
                    select(ResourceAllocation).where(ResourceAllocation.appointment_id == appt_id)
                )
            )
            .scalars()
            .all()
        )
        statuses = {a.status for a in allocs}
        assert "active" in statuses
        assert "released" in statuses
