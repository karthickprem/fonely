"""PostgreSQL integration test for conversation orchestrator booking flow."""

from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fonely.api.internal.validation import InternalValidationPort
from fonely.core.validators import utcnow
from fonely.domain.conversation.state import ConversationState
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.models.schema import Appointment, PendingAction, ResourceAllocation
from fonely.services.appointments import AppointmentService
from fonely.services.conversation import _CONVERSATIONS, ConversationService
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
    gw.complete.return_value = ModelResponse(text="Sure, let me help!")
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


@pytest.fixture(autouse=True)
def clear_conversations() -> None:
    _CONVERSATIONS.clear()


async def test_full_booking_flow_commits_appointment(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)

    now = utcnow()
    weekday = now.isoweekday()
    if weekday == 7:
        target = now + timedelta(days=1)
    else:
        target = now + timedelta(days=1)
        if target.isoweekday() == 7:
            target += timedelta(days=1)

    slot_start = datetime.combine(target.date(), time(10, 30), tzinfo=UTC)

    async with pg_session_factory() as session:
        gateway = _mock_gateway()
        validation = InternalValidationPort(session)
        appt_service = AppointmentService(session, validation=validation)
        conv_service = ConversationService(session, gateway, appointment_service=appt_service)

        turn1 = await conv_service.process_message(
            "booking-flow", 1, _actor(), "I want to book an appointment"
        )
        assert turn1.state == ConversationState.FACT_COLLECTION

        ctx = _CONVERSATIONS["booking-flow"]
        ctx.collected_facts["service_id"] = 1
        ctx.collected_facts["service_name"] = "General Consultation"
        ctx.collected_facts["resource_id"] = 1
        ctx.collected_facts["resource_name"] = "Dr. Priya"
        ctx.collected_facts["customer_phone"] = "+919123456789"
        ctx.collected_facts["start_at"] = slot_start

        await conv_service.process_message("booking-flow", 1, _actor(), "Let me check availability")
        assert ctx.proposal_id is not None

        pa_before = (
            await session.execute(select(PendingAction).where(PendingAction.id == ctx.proposal_id))
        ).scalar_one()
        assert pa_before.status in ("awaiting_confirmation", "committing")

        turn3 = await conv_service.process_message("booking-flow", 1, _actor(), "yes")
        assert turn3.state == ConversationState.COMPLETED
        assert "confirmed" in turn3.assistant_response.lower()

        appt = (
            await session.execute(
                select(Appointment).where(
                    Appointment.business_id == 1,
                    Appointment.service_id == 1,
                )
            )
        ).scalar_one()
        assert appt.resource_id == 1
        assert appt.status == "confirmed"
        assert appt.source == "customer_conversation"
        assert appt.pending_action_id is not None

        alloc = (
            await session.execute(
                select(ResourceAllocation).where(ResourceAllocation.appointment_id == appt.id)
            )
        ).scalar_one()
        assert alloc.status == "active"

        pa_after = (
            await session.execute(
                select(PendingAction).where(PendingAction.id == appt.pending_action_id)
            )
        ).scalar_one()
        assert pa_after.status == "confirmed"
        assert pa_after.committed_entity_id == appt.id


async def test_changed_mind_no_appointment(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)

    now = utcnow()
    target = now + timedelta(days=1)
    if target.isoweekday() == 7:
        target += timedelta(days=1)
    slot_start = datetime.combine(target.date(), time(10, 30), tzinfo=UTC)

    async with pg_session_factory() as session:
        gateway = _mock_gateway()
        validation = InternalValidationPort(session)
        appt_service = AppointmentService(session, validation=validation)
        conv_service = ConversationService(session, gateway, appointment_service=appt_service)

        await conv_service.process_message("changed-mind", 1, _actor(), "Book appointment")
        ctx = _CONVERSATIONS["changed-mind"]
        ctx.collected_facts.update(
            service_id=1,
            service_name="General Consultation",
            resource_id=1,
            resource_name="Dr. Priya",
            customer_phone="+919123456789",
            start_at=slot_start,
        )

        await conv_service.process_message("changed-mind", 1, _actor(), "Check slots")
        assert ctx.proposal_id is not None

        turn = await conv_service.process_message("changed-mind", 1, _actor(), "no, different time")
        assert turn.state == ConversationState.FACT_COLLECTION

        appt_count = await session.scalar(select(func.count(Appointment.id)))
        assert appt_count == 0
        await session.rollback()


async def test_medical_escalation_no_appointment(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)

    async with pg_session_factory() as session:
        gateway = _mock_gateway()
        validation = InternalValidationPort(session)
        appt_service = AppointmentService(session, validation=validation)
        conv_service = ConversationService(
            session, gateway, appointment_service=appt_service
        )

        await conv_service.process_message("medical-esc", 1, _actor(), "Book appointment")
        turn = await conv_service.process_message(
            "medical-esc", 1, _actor(), "My tooth is bleeding badly"
        )
        assert turn.state == ConversationState.ESCALATED
        assert turn.safety_classification == "medical"

        appt_count = await session.scalar(select(func.count(Appointment.id)))
        assert appt_count == 0
        await session.rollback()
