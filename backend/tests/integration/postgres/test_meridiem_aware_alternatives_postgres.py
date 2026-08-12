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
from tests.integration.postgres.conftest import seed_whatsapp_channel

pytestmark = pytest.mark.postgres

KOLKATA = ZoneInfo("Asia/Kolkata")


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


async def _seed_dual_window(session: AsyncSession) -> None:
    """Clinic open BOTH 05:00-06:00 and 17:00-18:00, so 5:30 AM and 5:30 PM
    both exist — the setup for a genuinely ambiguous bare "5:30"."""
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
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '05:00', '06:00', true FROM generate_series(0, 6) AS day"
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


async def _seed_offgrid_evening_clinic(session: AsyncSession) -> None:
    """Evening clinic 18:15-19:45, 30-min slots -> slots at 18:15/18:45/19:15.

    18:00 (the alt reading of a bare 6) is OFF-GRID here, so a bare six forces
    an OFFER (not an exact confirm) and exercises the full round-trip that both
    the lean-removal and the modulo-12 selection are load-bearing on.
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
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '18:15', '19:45', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


async def test_ambiguous_bare_time_asks_which_one_and_keeps_offer(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """D3-M3 item 1 (residual 1): a bare "5:30" matching both 5:30 AM and PM
    is asked about — the agent asks WHICH ONE and keeps the offer, rather than
    dropping the offer and asking for a date it already has."""
    async with pg_session_factory() as setup:
        await _seed_dual_window(setup)

    gw = _gw()
    # Turn 1: off-grid "5:45" bare -> nearest slot to 05:45 is 5:30 AM and to
    # 17:45 is 5:30 PM, so the meridiem-aware alternatives span BOTH 5:30s.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "book a general consultation with Dr. Priya tomorrow 5:45, "
                "reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    offer = ctx.collected_facts.get("_active_offer")
    assert isinstance(offer, dict)
    displays = {s["display_time"] for s in offer["slots"]}
    assert "5:30 AM" in displays and "5:30 PM" in displays, displays

    # Turn 2: a BARE "5:30" is ambiguous across the two meridiems.
    async with pg_session_factory() as session:
        response, _ = await _process_domain(_claimed(2, "5:30"), session, gw)
        await session.commit()

    ctx = _CONVERSATIONS[conv_id]
    # No premature selection; offer kept; the question is "which one".
    assert ctx.proposal_id is None
    assert "start_at" not in ctx.collected_facts
    assert "_active_offer" in ctx.collected_facts
    assert "which one" in response.lower()
    assert "5:30 am" in response.lower() and "5:30 pm" in response.lower()

    # Turn 3: the patient disambiguates -> selects and books.
    async with pg_session_factory() as session:
        await _process_domain(_claimed(3, "5:30 pm"), session, gw)
        await session.commit()
    ctx = _CONVERSATIONS[conv_id]
    assert ctx.proposal_id is not None
    assert "_selection_ambiguous" not in ctx.collected_facts

    async with pg_session_factory() as session:
        await _process_domain(_claimed(4, "yes confirm"), session, gw)
        await session.commit()

    async with pg_session_factory() as verify:
        row = (
            await verify.execute(
                text(
                    "SELECT start_at FROM appointments "
                    "WHERE business_id = 1 AND status = 'confirmed'"
                )
            )
        ).one_or_none()
        assert row is not None
        committed = row[0]
        if committed.tzinfo is None:
            committed = committed.replace(tzinfo=UTC)
        assert committed.astimezone(KOLKATA).hour == 17, "Must book the 5:30 PM slot"


async def test_out_of_hours_bare_time_still_reaches_a_bookable_slot(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """D3-M3 item 3: the load-bearing coupling guard.

    Lean-removal (bare 'aaru mani' -> 06:00 with meridiem_explicit=False) and
    modulo-12 offer disambiguation are load-bearing on each other. This test
    exercises the exact path where both matter: a bare Tamil evening hour whose
    literal reading (06:00) is CLOSED still reaches a bookable evening slot
    through the offer round-trip, and the patient's bare evening reply selects
    it via modulo-12.

    The coupling is guarded on both halves, each proven by reverting it alone:
    - Revert the MODULO-12 disambiguation (bare time -> exact-hour-only match):
      the patient's bare "6:15" no longer matches the 6:15 PM slot, so
      _selected_token stays None and the assertion below FAILS. (Verified.)
    - Revert the LEAN-REMOVAL (re-add small-hour -> +12 in the parser): the
      parser unit tests test_datetime_parse::test_tamil_tanglish[aaru mani] and
      ::test_meridiem_flag[aaru mani] FAIL, because 'aaru mani' becomes 18:00
      with meridiem_explicit=True instead of 06:00/False. (Verified.)
    Together those two guard points fail if either mechanism is removed.
    """
    async with pg_session_factory() as setup:
        await _seed_offgrid_evening_clinic(setup)

    gw = _gw()
    # Turn 1: bare Tamil evening hour "aaru mani" -> 06:00 (closed) and 18:00
    # (alt) which is OFF-GRID for this 18:15-19:45 clinic, so BOTH readings are
    # non-exact and an OFFER of evening alternatives (6:15/6:45 PM) is produced.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "book a general consultation with Dr. Priya tomorrow aaru mani, "
                "reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]

    # DEPENDS ON lean-removal + meridiem-aware alternatives: the literal 06:00
    # is closed, so recovery is only possible if the evening reading surfaced.
    offer = ctx.collected_facts.get("_active_offer")
    assert offer is not None, (
        "Out-of-hours bare time did not reach an offer — the meridiem-aware "
        "recovery is broken (lean-removal / alternatives coupling)."
    )
    assert isinstance(offer, dict)
    for slot in offer["slots"]:
        local = datetime.fromisoformat(slot["start_at_utc"]).astimezone(KOLKATA)
        assert time(18, 15) <= local.time() < time(19, 45)

    # DEPENDS ON modulo-12 selection: the patient replies with a BARE evening
    # time (6:15 -> 06:15) and it must land on the 6:15 PM slot via mod-12.
    async with pg_session_factory() as session:
        await _process_domain(_claimed(2, "6:15"), session, gw)
        await session.commit()

    ctx = _CONVERSATIONS[conv_id]
    assert ctx.proposal_id is not None, (
        "Bare evening reply did not select an offered slot — the modulo-12 "
        "disambiguation is broken."
    )
    # The selection MUST have gone through the offer token path — that is what
    # the modulo-12 match feeds. _selected_token is set only by
    # _try_offer_selection, never by a re-derivation fallback, so this pins the
    # test to the coupling under guard rather than a lucky recovery elsewhere.
    assert ctx.collected_facts.get("_selected_token") is not None, (
        "Reply booked without going through offer selection — modulo-12 broke "
        "and a re-derivation path masked it."
    )

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
        assert row is not None, "No booking — the out-of-hours recovery failed end to end."
        committed = row[0]
        if committed.tzinfo is None:
            committed = committed.replace(tzinfo=UTC)
        local = committed.astimezone(KOLKATA)
        assert local.date() == tomorrow
        assert time(18, 15) <= local.time() < time(19, 45)


async def test_split_turn_meridiem_survives_and_offers_evening(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DEFECT 1 regression: a bare evening hour given BEFORE the date still
    offers evening slots when the date arrives in a later turn.

    Patient says "6 mani" (time first, no date), we ask for the date, they say
    "naalaikku" (tomorrow). The alternate 18:00 reading must survive the
    _pending_time path so an evening clinic offers evening slots, not morning.
    """
    async with pg_session_factory() as setup:
        await _seed_evening_clinic(setup)

    gw = _gw()
    # Turn 1: time only, no date -> held as pending, no offer yet.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "book a general consultation with Dr. Priya at 6 mani, reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    assert ctx.collected_facts.get("_pending_time") == "06:00:00"
    assert ctx.collected_facts.get("_pending_time_explicit") is False
    assert "start_at" not in ctx.collected_facts

    # Turn 2: the date arrives in a SEPARATE turn.
    async with pg_session_factory() as session:
        await _process_domain(_claimed(2, "naalaikku"), session, gw)
        await session.commit()

    ctx = _CONVERSATIONS[conv_id]
    # The alternate reading survived, so the offer is EVENING, not morning.
    offer = ctx.collected_facts.get("_active_offer")
    assert offer is not None, (
        f"No offer after split-turn. State: {ctx.state}, facts: {list(ctx.collected_facts)}"
    )
    assert isinstance(offer, dict)
    for slot in offer["slots"]:
        local = datetime.fromisoformat(slot["start_at_utc"]).astimezone(KOLKATA)
        assert time(18, 0) <= local.time() < time(19, 30), (
            f"Split-turn offered a non-evening slot {local.time()} — defect 1 regressed."
        )


@pytest.mark.parametrize("answer", ["pm", "evening", "மாலை"])
async def test_ambiguity_resolved_by_bare_meridiem_word(
    pg_session_factory: async_sessionmaker[AsyncSession],
    answer: str,
) -> None:
    """DEFECT 2 regression: a bare meridiem answer resolves the which-one
    question in English, Tamil, and Tanglish — never an infinite loop."""
    async with pg_session_factory() as setup:
        await _seed_dual_window(setup)

    gw = _gw()
    # Off-grid 5:45 -> alternatives span both 5:30 AM and 5:30 PM.
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "book a general consultation with Dr. Priya tomorrow 5:45, "
                "reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()
    conv_id = next(iter(_CONVERSATIONS.keys()))

    # Bare "5:30" -> ambiguous, we ask which one.
    async with pg_session_factory() as session:
        await _process_domain(_claimed(2, "5:30"), session, gw)
        await session.commit()
    ctx = _CONVERSATIONS[conv_id]
    assert ctx.collected_facts.get("_selection_ambiguous")

    # The patient answers with a bare meridiem word -> resolves, no loop.
    async with pg_session_factory() as session:
        await _process_domain(_claimed(3, answer), session, gw)
        await session.commit()
    ctx = _CONVERSATIONS[conv_id]
    assert "_selection_ambiguous" not in ctx.collected_facts, (
        f"{answer!r} did not resolve the ambiguity — loop regressed."
    )
    assert ctx.proposal_id is not None, f"{answer!r} resolved but did not book."

    async with pg_session_factory() as session:
        await _process_domain(_claimed(4, "yes confirm"), session, gw)
        await session.commit()

    is_pm = answer in ("pm", "evening", "மாலை")
    async with pg_session_factory() as verify:
        row = (
            await verify.execute(
                text(
                    "SELECT start_at FROM appointments "
                    "WHERE business_id = 1 AND status = 'confirmed'"
                )
            )
        ).one_or_none()
        assert row is not None
        committed = row[0]
        if committed.tzinfo is None:
            committed = committed.replace(tzinfo=UTC)
        hour = committed.astimezone(KOLKATA).hour
        assert (hour >= 12) == is_pm, f"{answer!r} booked hour {hour}, expected PM={is_pm}"


async def test_ambiguity_question_is_bounded(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DEFECT 2 regression: unresolvable answers cannot loop forever — after a
    bounded number of asks the offer is dropped and a plain time question is
    asked, guaranteeing an exit."""
    async with pg_session_factory() as setup:
        await _seed_dual_window(setup)

    gw = _gw()
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "book a general consultation with Dr. Priya tomorrow 5:45, "
                "reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()
    conv_id = next(iter(_CONVERSATIONS.keys()))

    async with pg_session_factory() as session:
        await _process_domain(_claimed(2, "5:30"), session, gw)
        await session.commit()
    assert _CONVERSATIONS[conv_id].collected_facts.get("_selection_ambiguous")

    # Reply with something that neither resolves nor parses — no time, no
    # ordinal, and NO meridiem word (so it cannot accidentally resolve).
    last_response = ""
    for i in range(4):
        async with pg_session_factory() as session:
            last_response, _ = await _process_domain(
                _claimed(10 + i, "hmm not really sure"), session, gw
            )
            await session.commit()
        ctx = _CONVERSATIONS[conv_id]
        if "_selection_ambiguous" not in ctx.collected_facts:
            break

    ctx = _CONVERSATIONS[conv_id]
    # The loop must have exited via the bound: ambiguity cleared, offer dropped,
    # and the plain fallback time question asked.
    assert "_selection_ambiguous" not in ctx.collected_facts, (
        "Ambiguity question looped without bound — defect 2 regressed."
    )
    assert ctx.proposal_id is None
    assert "what time" in last_response.lower()


async def _seed_am_and_pm_windows(session: AsyncSession) -> None:
    """Clinic open 06:00-06:30 (AM slot 6:00) and 17:30-18:30 (PM slots 5:30,
    6:00). A bare six is ambiguous between 6:00 AM and 6:00 PM, and 5:30 PM
    merely shares the PM meridiem — the defect-3 trap."""
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
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '06:00', '06:30', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '17:30', '18:30', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


async def test_three_slot_ambiguity_books_the_asked_pm_not_the_other(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DEFECT 3 regression end-to-end: offered 6:00 AM / 5:30 PM / 6:00 PM,
    asked "6:00 AM or 6:00 PM?", the patient's "pm" books 6:00 PM — never the
    5:30 PM that merely shares the meridiem."""
    async with pg_session_factory() as setup:
        await _seed_am_and_pm_windows(setup)

    gw = _gw()
    # Off-grid 6:10 so neither reading is exact and the alternative set spans
    # both windows (6:00 AM, plus 5:30 PM and 6:00 PM near 18:10).
    async with pg_session_factory() as session:
        await _process_domain(
            _claimed(
                1,
                "book a general consultation with Dr. Priya tomorrow 6:10, "
                "reach me on +919123456789",
            ),
            session,
            gw,
        )
        await session.commit()

    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    offer = ctx.collected_facts.get("_active_offer")
    assert isinstance(offer, dict)
    displays = [s["display_time"] for s in offer["slots"]]
    assert "6:00 AM" in displays and "6:00 PM" in displays, displays

    # A bare six maps to 6:00 AM / 6:00 PM -> ambiguous between exactly those.
    async with pg_session_factory() as session:
        await _process_domain(_claimed(2, "6 o'clock"), session, gw)
        await session.commit()
    ctx = _CONVERSATIONS[conv_id]
    amb = ctx.collected_facts.get("_selection_ambiguous")
    assert amb, f"Expected ambiguity. State: {ctx.state}"
    assert {e["display"] for e in amb} == {"6:00 AM", "6:00 PM"}

    # "pm" must resolve to 6:00 PM, not the 5:30 PM that shares the meridiem.
    async with pg_session_factory() as session:
        await _process_domain(_claimed(3, "pm"), session, gw)
        await session.commit()
    ctx = _CONVERSATIONS[conv_id]
    assert ctx.proposal_id is not None

    async with pg_session_factory() as session:
        await _process_domain(_claimed(4, "yes confirm"), session, gw)
        await session.commit()

    async with pg_session_factory() as verify:
        row = (
            await verify.execute(
                text(
                    "SELECT start_at FROM appointments "
                    "WHERE business_id = 1 AND status = 'confirmed'"
                )
            )
        ).one_or_none()
        assert row is not None
        committed = row[0]
        if committed.tzinfo is None:
            committed = committed.replace(tzinfo=UTC)
        local = committed.astimezone(KOLKATA)
        assert (local.hour, local.minute) == (18, 0), (
            f"Booked {local.strftime('%-I:%M %p')}, expected 6:00 PM — defect 3 regressed."
        )
