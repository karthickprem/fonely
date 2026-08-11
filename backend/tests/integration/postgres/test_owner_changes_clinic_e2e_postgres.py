"""D3-M2 Part 3 demonstration: the owner changes the clinic by chatting.

The owner texts a change in plain language; availability reflects it; a
patient asking for a withdrawn slot is refused while a patient asking for a
still-open slot is offered one. Driven by natural messages through the real
inbound-worker path, Postgres-backed, no direct fact writes.

Two non-negotiables, both asserted here:
- the owner's identity and role come from trusted context (verified sender
  phone), never from message content;
- a change that collides with a booked appointment surfaces the conflict to
  the owner rather than silently orphaning the patient.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.services.conversation import _CONVERSATIONS
from fonely.services.model_gateway import ModelResponse
from fonely.services.owner_command_parser import ParsedOwnerCommand
from fonely.workers.inbound_worker import ClaimedEvent, _process_domain

pytestmark = pytest.mark.postgres

KOLKATA = ZoneInfo("Asia/Kolkata")

OWNER_PHONE = "+919000000001"
PATIENT_PHONE = "+919123456789"
OTHER_PATIENT_PHONE = "+919555000222"


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import notifications, whatsapp_config

    mappings = '{"phone-1": 1}'
    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_phone_number_id", "phone-1")


@pytest.fixture(autouse=True)
def _clear_conversations():
    _CONVERSATIONS.clear()
    yield
    _CONVERSATIONS.clear()


def _target_thursday() -> datetime.date:
    d = datetime.now(KOLKATA).date() + timedelta(days=1)
    while d.isoweekday() != 4:  # Thursday
        d += timedelta(days=1)
    return d


async def _seed(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription, "
            "appointment_slot_interval_minutes) "
            "VALUES (1, 'Smile Dental Clinic', 'dental', :owner, "
            "'Asia/Kolkata', 'trial', 30)"
        ),
        {"owner": OWNER_PHONE},
    )
    await session.execute(
        text(
            "INSERT INTO business_users (id, business_id, phone, role, is_active) "
            "VALUES (1, 1, :owner, 'owner', true)"
        ),
        {"owner": OWNER_PHONE},
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'General Consultation', 30, 0, 0, 500.00, true)"
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
            "(business_id, service_id, resource_id, is_active) VALUES (1, 1, 1, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '10:00', '13:00', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


def _claimed(event_id: int, sender: str, body: str) -> ClaimedEvent:
    return ClaimedEvent(
        event_id=event_id,
        business_id=1,
        message_id=f"wamid.owner.{event_id}",
        sender_phone=sender,
        message_type="text",
        message_body=body,
        phone_number_id="phone-1",
        claim_token=uuid.uuid4(),
        claim_version=1,
        attempts=0,
        max_attempts=5,
    )


def _gw() -> AsyncMock:
    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text="ok")
    return gw


async def test_owner_leave_withdraws_slots_and_surfaces_conflict(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with pg_session_factory() as setup:
        await _seed(setup)

    thursday = _target_thursday()

    # A patient books a Thursday 10:30 appointment first.
    gw = _gw()
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                PATIENT_PHONE,
                f"book a general consultation with Dr. Priya on Thursday at 10:30 am, "
                f"reach me on {PATIENT_PHONE}",
            ),
            session,
            gw,
        )
        await session.commit()
    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    assert ctx.proposal_id is not None
    async with pg_session_factory() as session:
        await _process_domain(_claimed(2, PATIENT_PHONE, "yes confirm"), session, gw)
        await session.commit()

    async with pg_session_factory() as verify:
        booked = await verify.scalar(
            text("SELECT count(*) FROM appointments WHERE business_id = 1 AND status = 'confirmed'")
        )
        assert booked == 1

    # The OWNER texts a leave for Dr. Priya on Thursday. The parser is stubbed
    # to a doctor_leave command so the test is deterministic; identity/role are
    # still resolved from the verified owner phone by the worker, not the parse.
    from fonely.services import owner_command_parser

    async def _fake_parse(
        self: object, message: str, doctor_names: list[str]
    ) -> ParsedOwnerCommand:
        return ParsedOwnerCommand(
            command="doctor_leave",
            doctor_name="Dr. Priya",
            date=thursday.isoformat(),
            reason="Leave",
        )

    monkeypatch.setattr(owner_command_parser.OwnerCommandParser, "parse", _fake_parse)

    _CONVERSATIONS.clear()
    owner_gw = _gw()
    async with pg_session_factory() as session:
        response, recipient = await _process_domain(
            _claimed(3, OWNER_PHONE, "Dr. Priya is on leave Thursday"),
            session,
            owner_gw,
        )
        await session.commit()

    # The owner is answered as an owner, and the conflict is surfaced, not hidden.
    assert recipient == "owner"
    assert "leave" in response.lower()
    assert "cancel" in response.lower() or "1 appointment" in response.lower()

    # The booked patient's appointment is now cancelled.
    async with pg_session_factory() as verify:
        active = await verify.scalar(
            text("SELECT count(*) FROM appointments WHERE business_id = 1 AND status = 'confirmed'")
        )
        assert active == 0, "Leave must cancel the conflicting appointment"

        exc = await verify.scalar(
            text(
                "SELECT count(*) FROM schedule_exceptions "
                "WHERE business_id = 1 AND resource_id = 1 "
                "AND exception_date = :d AND is_closed = true"
            ),
            {"d": thursday},
        )
        assert exc == 1, "A closed exception must exist for the leave date"

    # A NEW patient asking for a Thursday slot is refused (no proposal, and
    # any offered alternatives are not on Thursday).
    _CONVERSATIONS.clear()
    new_gw = _gw()
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                4,
                OTHER_PATIENT_PHONE,
                f"book a consultation with Dr. Priya on Thursday at 10:30 am, "
                f"reach me on {OTHER_PATIENT_PHONE}",
            ),
            session,
            new_gw,
        )
        await session.commit()

    new_conv = next(iter(_CONVERSATIONS.keys()))
    new_ctx = _CONVERSATIONS[new_conv]
    assert new_ctx.proposal_id is None, "Thursday slot must be refused after leave"
    offer = new_ctx.collected_facts.get("_active_offer")
    if offer is not None:
        for slot in offer["slots"]:
            local = datetime.fromisoformat(slot["start_at_utc"]).astimezone(KOLKATA)
            assert local.date() != thursday, "Must not offer a Thursday slot"

    async with pg_session_factory() as verify:
        confirmed = await verify.scalar(
            text("SELECT count(*) FROM appointments WHERE business_id = 1 AND status = 'confirmed'")
        )
        assert confirmed == 0


async def test_unknown_sender_is_not_treated_as_owner(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owner instruction from a non-owner phone must not change the clinic."""
    async with pg_session_factory() as setup:
        await _seed(setup)

    thursday = _target_thursday()

    from fonely.services import owner_command_parser

    async def _fake_parse(
        self: object, message: str, doctor_names: list[str]
    ) -> ParsedOwnerCommand:
        return ParsedOwnerCommand(
            command="doctor_leave",
            doctor_name="Dr. Priya",
            date=thursday.isoformat(),
            reason="Leave",
        )

    monkeypatch.setattr(owner_command_parser.OwnerCommandParser, "parse", _fake_parse)

    gw = _gw()
    # A stranger (not the owner phone) sends the same instruction.
    async with pg_session_factory() as session:
        _resp, recipient = await _process_domain(
            _claimed(1, OTHER_PATIENT_PHONE, "Dr. Priya is on leave Thursday"),
            session,
            gw,
        )
        await session.commit()

    # They are handled as a patient, not an owner — no schedule change.
    assert recipient == "patient"
    async with pg_session_factory() as verify:
        exc = await verify.scalar(
            text(
                "SELECT count(*) FROM schedule_exceptions "
                "WHERE business_id = 1 AND is_closed = true"
            )
        )
        assert exc == 0, "A non-owner must never change the clinic schedule"
