"""D3-M3 item 1: alternatives consider BOTH meridiem readings of a bare time.

A Tamil-speaking patient who says "aaru mani" (six o'clock) meaning 6 PM must
not be offered only morning slots. When am/pm is not explicit, the requested
time is considered under both readings (06:00 and 18:00); the reading that is
open dominates the offer, so the patient sees the evening slots they meant.
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


async def _seed_evening_clinic(session: AsyncSession) -> None:
    """Evening-only clinic: 18:00-19:30, 30-min slots. Morning is closed."""
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
            "SELECT 1, day, '18:00', '19:30', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


def _claimed(event_id: int, body: str) -> ClaimedEvent:
    return ClaimedEvent(
        event_id=event_id,
        business_id=1,
        message_id=f"wamid.mer.{event_id}",
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


@pytest.mark.parametrize(
    "evening_phrase",
    ["aaru mani", "6", "six o'clock"],
)
async def test_bare_evening_hour_offers_evening_slots(
    pg_session_factory: async_sessionmaker[AsyncSession],
    evening_phrase: str,
) -> None:
    """A bare six-o'clock at an evening clinic is offered evening slots.

    18:00 is open, 06:00 is closed. The meridiem-aware alternatives must
    surface the 6 PM neighbourhood (18:00 / 18:30), NOT the morning.
    """
    async with pg_session_factory() as setup:
        await _seed_evening_clinic(setup)

    gw = _gw()
    # The patient asks for the clinic tomorrow at a bare six o'clock, with an
    # off-grid minute so an OFFER (not an exact confirm) is produced.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                f"book a general consultation with Dr. Priya tomorrow "
                f"{evening_phrase} 15, reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    offer = ctx.collected_facts.get("_active_offer")
    assert offer is not None, (
        f"No offer for {evening_phrase!r}. State: {ctx.state}, facts: {list(ctx.collected_facts)}"
    )
    assert isinstance(offer, dict)
    assert offer["slots"], "An offer with no slots must not be stored"

    # Every offered slot is in the evening window — the morning half the
    # patient did NOT mean must never appear.
    for slot in offer["slots"]:
        local = datetime.fromisoformat(slot["start_at_utc"]).astimezone(KOLKATA)
        assert time(18, 0) <= local.time() < time(19, 30), (
            f"{evening_phrase!r} offered a non-evening slot: {local.time()}"
        )


async def test_bare_evening_hour_books_evening_via_full_round_trip(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Full transcript: bare Tamil evening hour -> evening offer -> commit 6 PM."""
    async with pg_session_factory() as setup:
        await _seed_evening_clinic(setup)

    gw = _gw()
    # Turn 1: bare "aaru mani" (six) with off-grid minute -> evening offer.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "book a general consultation with Dr. Priya tomorrow aaru mani 15, "
                "reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    offer = ctx.collected_facts["_active_offer"]
    assert isinstance(offer, dict)
    offered = [
        datetime.fromisoformat(s["start_at_utc"]).astimezone(KOLKATA).strftime("%-I:%M %p")
        for s in offer["slots"]
    ]
    # The offered choices are the evening ones the patient meant.
    assert any("6:00 PM" in o or "6:30 PM" in o for o in offered), offered

    # Turn 2: the patient names a bare evening time; modulo-12 selects the PM.
    async with pg_session_factory() as session:
        await _process_domain(_claimed(2, "6:30"), session, gw)
        await session.commit()

    ctx = _CONVERSATIONS[conv_id]
    assert ctx.proposal_id is not None, f"No proposal. State: {ctx.state}"

    async with pg_session_factory() as session:
        await _process_domain(_claimed(3, "yes confirm"), session, gw)
        await session.commit()

    tomorrow = (datetime.now(KOLKATA) + timedelta(days=1)).date()

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
        local = committed.astimezone(KOLKATA)
        # The committed slot is in the EVENING window on TOMORROW — the patient
        # who said a bare evening six is booked into the evening, never morning.
        assert local.date() == tomorrow
        assert time(18, 0) <= local.time() < time(19, 30), (
            f"Committed {local.time()} is not an evening slot"
        )
