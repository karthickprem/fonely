"""P0 regression: a bare time must never book the wrong day.

The M1 review found that a patient offered slots for TOMORROW who replies
"10:30" (no am/pm) fell through to a raw parser that defaulted the date to
today and booked TODAY. These tests invert that bug:

- a bare time after a TOMORROW offer selects the offered slot on TOMORROW;
- a bare time with no active offer and no known date books nothing at all,
  because the parser is not allowed to invent a date.
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.services.conversation import _CONVERSATIONS
from fonely.services.model_gateway import ModelResponse
from fonely.workers.inbound_worker import ClaimedEvent, _process_domain

pytestmark = pytest.mark.postgres

KOLKATA = ZoneInfo("Asia/Kolkata")


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


async def _seed(session: AsyncSession) -> None:
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
            "SELECT 1, day, '10:00', '11:00', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


def _claimed(event_id: int, body: str) -> ClaimedEvent:
    return ClaimedEvent(
        event_id=event_id,
        business_id=1,
        message_id=f"wamid.bare.{event_id}",
        sender_phone="+919123456789",
        message_type="text",
        message_body=body,
        phone_number_id="phone-1",
        claim_token=uuid.uuid4(),
        claim_version=1,
        attempts=0,
        max_attempts=5,
    )


async def _seed_evening(session: AsyncSession) -> None:
    """Same clinic but open only in the evening: 17:00-18:00, 30-min slots.

    Offered slots are 5:00 PM and 5:30 PM — the case where a bare "5:30"
    reply parses as 05:30 and must still select the 5:30 PM slot.
    """
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
            "SELECT 1, day, '17:00', '18:00', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


def _gw() -> AsyncMock:
    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text="ok")
    return gw


@pytest.mark.parametrize("bare_reply", ["10:30", "ok 10:30", "10.30", "half past ten"])
async def test_bare_time_books_the_offered_day_not_today(
    pg_session_factory: async_sessionmaker[AsyncSession],
    bare_reply: str,
) -> None:
    async with pg_session_factory() as setup:
        await _seed(setup)

    gw = _gw()
    # Turn 1: off-grid TOMORROW request -> offered 10:00/10:30 for TOMORROW.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "I want to book a general consultation with Dr. Priya tomorrow "
                "at 10:15 am, reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    assert "_active_offer" in ctx.collected_facts

    # Turn 2: patient replies with a BARE time (no am/pm, no "tomorrow").
    async with pg_session_factory() as session:
        await _process_domain(_claimed(2, bare_reply), session, gw)
        await session.commit()

    ctx = _CONVERSATIONS[conv_id]
    assert ctx.proposal_id is not None, (
        f"Bare reply {bare_reply!r} did not select. State: {ctx.state}"
    )
    # The selection went through the offer token path.
    assert ctx.collected_facts.get("_selected_token") is not None

    async with pg_session_factory() as session:
        await _process_domain(_claimed(3, "yes confirm"), session, gw)
        await session.commit()

    tomorrow = (datetime.now(KOLKATA) + timedelta(days=1)).date()
    expected = datetime.combine(tomorrow, time(10, 30), tzinfo=KOLKATA).astimezone(UTC)

    async with pg_session_factory() as verify:
        row = (
            await verify.execute(
                text(
                    "SELECT start_at FROM appointments "
                    "WHERE business_id = 1 AND status = 'confirmed'"
                )
            )
        ).one_or_none()
        assert row is not None, "No confirmed appointment"
        committed = row[0]
        if committed.tzinfo is None:
            committed = committed.replace(tzinfo=UTC)
        assert committed == expected, (
            f"Bare {bare_reply!r} booked {committed.astimezone(KOLKATA)}, expected TOMORROW 10:30"
        )


async def test_bare_time_with_no_date_books_nothing(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A bare time, no offer and no date, must not compose a datetime."""
    async with pg_session_factory() as setup:
        await _seed(setup)

    gw = _gw()
    # Patient gives service + resource + phone but only a bare TIME, no date.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "book a general consultation with Dr. Priya at 10:30, reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]

    # No date was given, so start_at must not be composed and nothing booked.
    assert "start_at" not in ctx.collected_facts
    assert ctx.proposal_id is None

    async with pg_session_factory() as verify:
        count = await verify.scalar(text("SELECT count(*) FROM appointments WHERE business_id = 1"))
        assert count == 0, "A bare time with no date must book nothing"


@pytest.mark.parametrize(
    "vague",
    ["sometime in the afternoon-ish", "whenever you have space", "asap please"],
)
async def test_vague_time_is_not_guessed(
    pg_session_factory: async_sessionmaker[AsyncSession],
    vague: str,
) -> None:
    """An unparseable time produces no datetime — the agent must ask, not guess."""
    async with pg_session_factory() as setup:
        await _seed(setup)

    gw = _gw()
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                f"book a consultation with Dr. Priya tomorrow {vague}, reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    # A vague time is not resolved -> no start_at, no proposal, no booking.
    assert "start_at" not in ctx.collected_facts
    assert ctx.proposal_id is None

    async with pg_session_factory() as verify:
        count = await verify.scalar(text("SELECT count(*) FROM appointments WHERE business_id = 1"))
        assert count == 0


@pytest.mark.parametrize(
    "bare_reply",
    ["5:30", "ok 5:30", "5.30", "half past five", "5:30 in the evening"],
)
async def test_bare_evening_time_selects_the_pm_offer(
    pg_session_factory: async_sessionmaker[AsyncSession],
    bare_reply: str,
) -> None:
    """BLOCKER regression: a bare "5:30" reply selects the 5:30 PM offered slot.

    Evening clinic (17:00-18:00) offers 5:00 PM and 5:30 PM. The patient's
    "5:30" parses as 05:30 with no meridiem; the offer-set disambiguation must
    match it (mod 12) to the 5:30 PM slot and book TOMORROW 17:30 — not fail
    selection, discard the offer, and then reject 05:30 as outside hours.
    """
    async with pg_session_factory() as setup:
        await _seed_evening(setup)

    gw = _gw()
    # Turn 1: off-grid TOMORROW evening request -> offered 5:00/5:30 PM tomorrow.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "I want to book a general consultation with Dr. Priya tomorrow "
                "at 5:15 pm, reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    assert "_active_offer" in ctx.collected_facts, (
        f"No evening offer. State: {ctx.state}, facts: {list(ctx.collected_facts)}"
    )

    # Turn 2: bare evening time (no am/pm).
    async with pg_session_factory() as session:
        await _process_domain(_claimed(2, bare_reply), session, gw)
        await session.commit()

    ctx = _CONVERSATIONS[conv_id]
    assert ctx.proposal_id is not None, (
        f"Bare evening reply {bare_reply!r} did not select. State: {ctx.state}"
    )
    assert ctx.collected_facts.get("_selected_token") is not None

    async with pg_session_factory() as session:
        await _process_domain(_claimed(3, "yes confirm"), session, gw)
        await session.commit()

    tomorrow = (datetime.now(KOLKATA) + timedelta(days=1)).date()
    expected = datetime.combine(tomorrow, time(17, 30), tzinfo=KOLKATA).astimezone(UTC)

    async with pg_session_factory() as verify:
        row = (
            await verify.execute(
                text(
                    "SELECT start_at FROM appointments "
                    "WHERE business_id = 1 AND status = 'confirmed'"
                )
            )
        ).one_or_none()
        assert row is not None, "No confirmed appointment"
        committed = row[0]
        if committed.tzinfo is None:
            committed = committed.replace(tzinfo=UTC)
        assert committed == expected, (
            f"Bare {bare_reply!r} booked {committed.astimezone(KOLKATA)}, expected TOMORROW 5:30 PM"
        )
