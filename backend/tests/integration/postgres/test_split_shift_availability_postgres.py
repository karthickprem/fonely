"""Split-shift reality: the clinic closes midday and on Sundays.

An Indian clinic open 09:30-13:00 and 17:00-20:30 must never offer a slot
in the 13:00-17:00 gap, and a Sunday-closed clinic must never offer Sunday.
Driven by natural messages against real availability.
"""

import uuid
from datetime import date, datetime, time, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.services.conversation import _CONVERSATIONS
from fonely.services.model_gateway import ModelResponse
from fonely.workers.inbound_worker import ClaimedEvent, _process_domain
from tests.integration.postgres.conftest import seed_whatsapp_channel

pytestmark = pytest.mark.postgres

KOLKATA = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _clear_conversations():
    _CONVERSATIONS.clear()
    yield
    _CONVERSATIONS.clear()


async def _seed_split_shift(session: AsyncSession) -> None:
    """09:30-13:00 and 17:00-20:30 Mon-Sat; closed Sunday (day_of_week 0)."""
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription, "
            "appointment_slot_interval_minutes) "
            "VALUES (1, 'Smile Dental Clinic', 'dental', '+919000000001', "
            "'Asia/Kolkata', 'trial', 30)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users (id, business_id, phone, role, is_active) "
            "VALUES (1, 1, '+919000000001', 'owner', true)"
        )
    )
    # WhatsApp channel identity moved into business_whatsapp_channels in 0016.
    await seed_whatsapp_channel(session, phone_number_id="phone-1")
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
    # Two windows per weekday (Mon=1 .. Sat=6). Sunday (0) intentionally absent.
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '09:30', '13:00', true FROM generate_series(1, 6) AS day"
        )
    )
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '17:00', '20:30', true FROM generate_series(1, 6) AS day"
        )
    )
    await session.commit()


def _claimed(event_id: int, body: str) -> ClaimedEvent:
    return ClaimedEvent(
        event_id=event_id,
        business_id=1,
        message_id=f"wamid.shift.{event_id}",
        sender_phone="+919123456789",
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


def _next_weekday(base: date) -> date:
    d = base + timedelta(days=1)
    while d.isoweekday() == 7:  # skip Sunday
        d += timedelta(days=1)
    return d


async def test_no_slot_offered_inside_midday_gap(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await _seed_split_shift(setup)

    gw = _gw()
    day = _next_weekday(datetime.now(KOLKATA).date())
    # Patient asks for 3pm — squarely inside the 13:00-17:00 closure.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                f"book a general consultation with Dr. Priya on {day.strftime('%A')} "
                "at 3 pm, reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]

    # 3pm is in the gap: no proposal, and any offered alternatives must all lie
    # OUTSIDE 13:00-17:00.
    assert ctx.proposal_id is None
    offer = ctx.collected_facts.get("_active_offer")
    if offer is not None:
        assert isinstance(offer, dict)
        for slot in offer["slots"]:
            local = datetime.fromisoformat(slot["start_at_utc"]).astimezone(KOLKATA)
            assert not (time(13, 0) <= local.time() < time(17, 0)), (
                f"Offered a slot in the midday gap: {local}"
            )


async def test_afternoon_slot_offered_is_in_evening_window(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await _seed_split_shift(setup)

    gw = _gw()
    day = _next_weekday(datetime.now(KOLKATA).date())
    # Off-grid 17:15 -> alternatives must be inside the evening window only.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                f"consultation with Dr. Priya {day.strftime('%A')} at 5:15 pm, "
                "reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    # The guarantee under test: any slot offered for a 17:15 request lies in
    # the evening window and never in the morning window or the midday gap.
    # (Alternatives may legitimately be empty if the wall-clock leaves no
    # evening slot on the resolved day; the invariant is about WHERE, not
    # WHETHER, slots are offered.)
    offer = ctx.collected_facts.get("_active_offer")
    if offer is not None:
        assert offer["slots"], "An offer with no slots should not be stored"
        for slot in offer["slots"]:
            local = datetime.fromisoformat(slot["start_at_utc"]).astimezone(KOLKATA)
            assert time(17, 0) <= local.time() < time(20, 30), (
                f"Evening-window slot expected, got {local}"
            )


async def test_no_slot_offered_on_closed_sunday(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await _seed_split_shift(setup)

    gw = _gw()
    # Find the next Sunday.
    d = datetime.now(KOLKATA).date() + timedelta(days=1)
    while d.isoweekday() != 7:
        d += timedelta(days=1)

    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "book a general consultation with Dr. Priya on Sunday at 10 am, "
                "reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]

    assert ctx.proposal_id is None
    offer = ctx.collected_facts.get("_active_offer")
    if offer is not None:
        for slot in offer["slots"]:
            local = datetime.fromisoformat(slot["start_at_utc"]).astimezone(KOLKATA)
            assert local.isoweekday() != 7, f"Offered a Sunday slot: {local}"

    async with pg_session_factory() as verify:
        count = await verify.scalar(text("SELECT count(*) FROM appointments WHERE business_id = 1"))
        assert count == 0
