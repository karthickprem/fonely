"""Offer binding, invalidation, and cross-tenant rejection.

Acceptance conditions 3 and 4 of the M1 rework:
- service/resource/date change each drop the active offer
- a cross-tenant offer is rejected using trusted context ids
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
from tests.integration.postgres.conftest import seed_whatsapp_channel

pytestmark = pytest.mark.postgres

KOLKATA = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _clear_conversations():
    _CONVERSATIONS.clear()
    yield
    _CONVERSATIONS.clear()


async def _seed_two_service_clinic(session: AsyncSession) -> None:
    """Clinic with two services, two doctors, narrow hours (offer forced)."""
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
            "buffer_after_minutes, price, is_active) VALUES "
            "(1, 1, 'General Consultation', 30, 0, 0, 500.00, true), "
            "(2, 1, 'Root Canal', 30, 0, 0, 6500.00, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Dr. Priya', 'staff', true), "
            "(2, 1, 'Dr. Rajesh', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) VALUES "
            "(1, 1, 1, true), (1, 2, 2, true), (1, 1, 2, true)"
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
        message_id=f"wamid.inval.{event_id}",
        sender_phone="+919123456789",
        message_type="text",
        message_body=body,
        phone_number_id="phone-1",
        claim_token=uuid.uuid4(),
        claim_version=1,
        attempts=0,
        max_attempts=5,
    )


def _mock_gateway() -> AsyncMock:
    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text="Sure!")
    return gw


async def _establish_offer(
    pg_session_factory: async_sessionmaker[AsyncSession], gateway: AsyncMock
) -> str:
    """Request Consultation with Dr. Priya at off-grid time -> active offer."""
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "I want to book a general consultation with Dr. Priya tomorrow "
                "at 10:15 am, reach me on +919123456789",
            ),
            session,
            gateway,
        )
        await session.commit()
    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    assert "_active_offer" in ctx.collected_facts
    return conv_id


async def test_service_change_invalidates_offer(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await _seed_two_service_clinic(setup)
    gateway = _mock_gateway()
    conv_id = await _establish_offer(pg_session_factory, gateway)

    # Patient switches to a different service
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(2, "actually make it a root canal instead"),
            session,
            gateway,
        )
        await session.commit()

    ctx = _CONVERSATIONS[conv_id]
    assert "_active_offer" not in ctx.collected_facts, "Offer must be invalidated on service change"


async def test_resource_change_invalidates_offer(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await _seed_two_service_clinic(setup)
    gateway = _mock_gateway()
    conv_id = await _establish_offer(pg_session_factory, gateway)

    # Patient switches doctor (Dr. Rajesh also eligible for consultation)
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(2, "can I see Dr. Rajesh instead"),
            session,
            gateway,
        )
        await session.commit()

    ctx = _CONVERSATIONS[conv_id]
    assert "_active_offer" not in ctx.collected_facts, (
        "Offer must be invalidated on resource change"
    )


async def test_date_change_invalidates_offer(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await _seed_two_service_clinic(setup)
    gateway = _mock_gateway()
    conv_id = await _establish_offer(pg_session_factory, gateway)

    ctx = _CONVERSATIONS[conv_id]
    old_offer = ctx.collected_facts["_active_offer"]
    assert isinstance(old_offer, dict)
    old_offer_id = old_offer["offer_id"]

    # Patient names a different off-grid time -> the requested slot changed,
    # so the prior offer must not survive as a selectable set.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(2, "actually make it 10:45 am"),
            session,
            gateway,
        )
        await session.commit()

    ctx = _CONVERSATIONS[conv_id]
    new_offer = ctx.collected_facts.get("_active_offer")
    # Either the stale offer was cleared, or a genuinely new offer replaced
    # it — in no case may the old offer_id still be the active selectable set.
    if new_offer is not None:
        assert isinstance(new_offer, dict)
        assert new_offer["offer_id"] != old_offer_id, (
            "Stale offer must not survive a date/time change"
        )


async def test_cross_tenant_offer_rejected(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An offer whose stored ids differ from trusted context is rejected."""
    from fonely.domain.booking.offers import (
        OfferValidationError,
        build_offer,
        serialize_offer,
        validate_selection,
    )

    slot_start = datetime.combine(
        (datetime.now(KOLKATA) + timedelta(days=1)).date(),
        time(10, 30),
        tzinfo=KOLKATA,
    ).astimezone(UTC)
    offer = build_offer(
        business_id=1,
        conversation_id="legit-conv",
        service_id=1,
        service_name="General Consultation",
        resource_id=1,
        resource_name="Dr. Priya",
        target_date=slot_start.date().isoformat(),
        available_slots=[{"start_at": slot_start, "end_at": slot_start + timedelta(minutes=30)}],
        business_timezone="Asia/Kolkata",
    )
    token = offer.slots[0].token

    # Same offer, but validated against a DIFFERENT trusted tenant/conversation
    with pytest.raises(OfferValidationError) as exc:
        validate_selection(offer, token, business_id=777, conversation_id="legit-conv")
    assert exc.value.code == "cross_tenant"

    with pytest.raises(OfferValidationError) as exc:
        validate_selection(offer, token, business_id=1, conversation_id="attacker-thread")
    assert exc.value.code == "cross_conversation"

    # Round-trip through serialization must preserve the binding
    data = serialize_offer(offer)
    from fonely.domain.booking.offers import deserialize_offer

    restored = deserialize_offer(data)
    assert restored is not None
    with pytest.raises(OfferValidationError):
        validate_selection(restored, token, business_id=777, conversation_id="legit-conv")
