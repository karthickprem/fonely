"""Full booking journey driven by natural patient messages only.

No direct writes to collected_facts. Facts are extracted from the
patient's message text through the conversation engine. The patient is
offered real availability alternatives and selects one by naming its
time — proving offer selection books the time the patient asked for.
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


async def _seed_clinic_narrow_hours(session: AsyncSession) -> None:
    """Clinic open 10:00-11:00 daily, 30-min slots. Only 10:00 and 10:30 exist."""
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
        message_id=f"wamid.journey.{event_id}",
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
    gw.complete.return_value = ModelResponse(text="Sure, let me help!")
    return gw


async def _run_offer_arc(
    pg_session_factory: async_sessionmaker[AsyncSession],
    gateway: AsyncMock,
    *,
    start_event_id: int,
    selection_text: str,
) -> None:
    """Drive the shared offer-selection arc through natural messages.

    Turn 1: request an off-grid time (10:15) -> offered 10:00/10:30.
    Turn 2: patient names an offered time -> proposal.
    Turn 3: explicit confirm -> commit.
    """
    # Turn 1: off-grid request with all other facts in natural text.
    # Phrasing avoids the safety classifier's medical false-positives
    # (e.g. "numb" inside "number") which are outside this milestone's scope.
    async with pg_session_factory() as session:
        _resp, recip = await _process_domain(
            _claimed(
                start_event_id,
                "I want to book a general consultation with Dr. Priya tomorrow "
                "at 10:15 am, reach me on +919123456789",
            ),
            session,
            gateway,
        )
        assert recip == "patient"
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    # 10:15 is off-grid -> no proposal yet, an offer must be active
    assert ctx.proposal_id is None
    assert "_active_offer" in ctx.collected_facts, (
        f"No active offer stored. Facts: {list(ctx.collected_facts.keys())}, State: {ctx.state}"
    )
    active_offer = ctx.collected_facts["_active_offer"]
    assert isinstance(active_offer, dict)
    offer_id_before = active_offer["offer_id"]
    slot_tokens = {s["display_time"]: s["token"] for s in active_offer["slots"]}
    # No selection markers may exist before the patient chooses.
    assert "_selected_token" not in ctx.collected_facts

    # Turn 2: patient names an offered time
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(start_event_id + 1, selection_text),
            session,
            gateway,
        )
        await session.commit()

    ctx = _CONVERSATIONS[conv_id]
    assert ctx.proposal_id is not None, (
        f"No proposal after selection. State: {ctx.state}, "
        f"Facts: {list(ctx.collected_facts.keys())}"
    )
    # Prove the time was reached THROUGH the offer, not the raw datetime parser.
    # _selected_token/_selected_offer_id are set only by _try_offer_selection;
    # the raw fallback never sets them. Their presence, bound to the active
    # offer, is the proof that the patient's selection went through validation.
    assert "_selected_token" in ctx.collected_facts, (
        "Selection did not go through the offer token path"
    )
    assert ctx.collected_facts["_selected_offer_id"] == offer_id_before
    assert ctx.collected_facts["_selected_token"] in slot_tokens.values()

    # Turn 3: explicit confirmation
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(start_event_id + 2, "yes confirm"),
            session,
            gateway,
        )
        await session.commit()


async def test_patient_books_the_offered_time_they_named(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Committed appointment is the offered slot the patient named (10:30)."""
    async with pg_session_factory() as setup:
        await _seed_clinic_narrow_hours(setup)

    gateway = _mock_gateway()
    await _run_offer_arc(
        pg_session_factory,
        gateway,
        start_event_id=1,
        selection_text="let's do 10:30 am please",
    )

    tomorrow = (datetime.now(KOLKATA) + timedelta(days=1)).date()
    expected_utc = datetime.combine(tomorrow, time(10, 30), tzinfo=KOLKATA).astimezone(UTC)

    async with pg_session_factory() as verify:
        row = (
            await verify.execute(
                text(
                    "SELECT id, status, service_name_snapshot, "
                    "resource_name_snapshot, start_at "
                    "FROM appointments WHERE business_id = 1 AND status = 'confirmed'"
                )
            )
        ).one_or_none()
        assert row is not None, "No confirmed appointment"
        assert row[2] == "General Consultation"
        assert row[3] == "Dr. Priya"
        committed_start = row[4]
        if committed_start.tzinfo is None:
            committed_start = committed_start.replace(tzinfo=UTC)
        assert committed_start == expected_utc, (
            f"Patient named 10:30 but committed {committed_start.astimezone(KOLKATA)}"
        )
        appt_id = row[0]

        manifest_count = await verify.scalar(
            text("SELECT count(*) FROM notification_manifests WHERE entity_id = :aid"),
            {"aid": appt_id},
        )
        assert manifest_count == 1

        outbox_count = await verify.scalar(
            text(
                "SELECT count(*) FROM notification_outbox "
                "WHERE entity_id = :aid AND event_type = 'appointment_confirmed'"
            ),
            {"aid": appt_id},
        )
        assert outbox_count >= 2  # patient + owner


async def test_patient_selects_by_ordinal(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Patient selects the first offered slot by ordinal word."""
    async with pg_session_factory() as setup:
        await _seed_clinic_narrow_hours(setup)

    gateway = _mock_gateway()
    await _run_offer_arc(
        pg_session_factory,
        gateway,
        start_event_id=1,
        selection_text="the first one works",
    )

    async with pg_session_factory() as verify:
        count = await verify.scalar(
            text("SELECT count(*) FROM appointments WHERE business_id = 1 AND status = 'confirmed'")
        )
        assert count == 1


async def test_duplicate_replay_no_double_booking(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Replaying the confirmation creates no second appointment."""
    async with pg_session_factory() as setup:
        await _seed_clinic_narrow_hours(setup)

    gateway = _mock_gateway()
    await _run_offer_arc(
        pg_session_factory,
        gateway,
        start_event_id=1,
        selection_text="10:30 am",
    )

    async with pg_session_factory() as verify:
        assert (
            await verify.scalar(text("SELECT count(*) FROM appointments WHERE business_id = 1"))
            == 1
        )

    # Replay the confirmation message
    async with pg_session_factory() as session:
        await _process_domain(_claimed(99, "yes confirm"), session, gateway)
        await session.commit()

    async with pg_session_factory() as verify:
        assert (
            await verify.scalar(text("SELECT count(*) FROM appointments WHERE business_id = 1"))
            == 1
        ), "Duplicate booking on replay"
