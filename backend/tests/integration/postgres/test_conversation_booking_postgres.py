"""PostgreSQL integration test for conversation orchestrator booking flow."""

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
from fonely.models.schema import Appointment, PendingAction, ResourceAllocation
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

    clinic_tz = ZoneInfo("Asia/Kolkata")
    slot_start = datetime.combine(target.date(), time(10, 30), tzinfo=clinic_tz).astimezone(UTC)

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
    clinic_tz = ZoneInfo("Asia/Kolkata")
    slot_start = datetime.combine(target.date(), time(10, 30), tzinfo=clinic_tz).astimezone(UTC)

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
        conv_service = ConversationService(session, gateway, appointment_service=appt_service)

        await conv_service.process_message("medical-esc", 1, _actor(), "Book appointment")
        turn = await conv_service.process_message(
            "medical-esc", 1, _actor(), "My tooth is bleeding badly"
        )
        assert turn.state == ConversationState.ESCALATED
        assert turn.safety_classification == "medical"

        appt_count = await session.scalar(select(func.count(Appointment.id)))
        assert appt_count == 0
        await session.rollback()


def _voice_actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.VOICE,
    )


async def _seed_two_priyas(session: AsyncSession) -> None:
    """Two doctors sharing a first name, so a bare 'priya' answer stays
    ambiguous and repeated unresolvable answers drive the give-up ladder."""
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
            "VALUES (1, 1, 'Dr. Priya Kumar', 'staff', true), "
            "(2, 1, 'Dr. Priya Rao', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) "
            "VALUES (1, 1, 1, true), (1, 1, 2, true)"
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
    await session.commit()


async def _drive_to_disambiguation_giveup(
    conv_service: ConversationService, conv_id: str, actor: ActorContext
) -> str:
    """Reach the terminal give-up and return THAT turn's text.

    Name an ambiguous doctor, then answer unresolvably. The ladder is plain,
    plain, numbered, then TERMINATE (ENDED). We return the give-up turn itself,
    not a turn past it — one more message after ENDED falls through to the mock
    gateway (the harness caveat: constant mock response), which would mask the
    real terminal wording. Bounded so an unexpectedly non-terminating ladder
    fails loudly rather than looping.
    """
    await conv_service.process_message(
        conv_id,
        1,
        actor,
        "i want General Consultation with dr priya tomorrow 10 30 am reach me on 9123456789",
    )
    for _ in range(6):
        turn = await conv_service.process_message(conv_id, 1, actor, "priya")
        if turn.state == ConversationState.ENDED:
            return turn.assistant_response
    raise AssertionError("disambiguation ladder did not terminate within 6 answers")


@pytest.mark.parametrize(
    "actor_factory,channel_id",
    [(_actor, "text"), (_voice_actor, "voice")],
    ids=["text", "voice"],
)
async def test_disambiguation_giveup_wording_is_channel_appropriate(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
    actor_factory,
    channel_id: str,
) -> None:
    """CEO #33: the terminal give-up wording must be truthful for the channel.

    On voice the caller is already connected to the clinic, so telling them to
    "call the clinic" is a false instruction; and we have no transfer/callback,
    so the voice wording must promise neither. On text, "call the clinic" stands.
    """
    async with pg_session_factory() as session:
        await _seed_two_priyas(session)

    async with pg_session_factory() as session:
        gateway = _mock_gateway()
        validation = InternalValidationPort(session)
        appt_service = AppointmentService(session, validation=validation)
        conv_service = ConversationService(session, gateway, appointment_service=appt_service)

        final = await _drive_to_disambiguation_giveup(
            conv_service, f"giveup-{channel_id}", actor_factory()
        )

        # Nothing booked either way (fail closed).
        appt_count = await session.scalar(select(func.count(Appointment.id)))
        assert appt_count == 0

        lowered = final.lower()
        if channel_id == "voice":
            # The patient-visible false instruction: telling a connected caller
            # to phone the clinic they are already on. Must NOT appear on voice.
            assert "call the clinic" not in lowered, (
                f"FALSE INSTRUCTION: a VOICE caller is already connected to the "
                f"clinic; telling them to call it strands them. Got: {final!r}"
            )
            # And it must not promise a capability we do not have.
            assert "transfer" not in lowered and "call you back" not in lowered, (
                f"FALSE PROMISE: no transfer/callback capability exists; the voice "
                f"give-up must promise neither. Got: {final!r}"
            )
            # It must still say the booking did not complete.
            assert "book" in lowered or "appointment" in lowered
        else:
            # Text: the clinic-call instruction is truthful and expected.
            assert "call the clinic" in lowered, final
        await session.rollback()
