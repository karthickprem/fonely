"""D3-M4: adversarial harness driving the REAL conversation path with
speech-shaped input, asserting invariants on EVERY input in the corpus.

Every prior probe against this component — mine and the reviewer's — was
text-shaped and mostly unit-level. The demo is a dentist speaking Tamil into a
phone, so the harness feeds speech-shaped utterances (no punctuation/capitals,
disfluencies, STT artifacts, code-mixing, split turns, negations, corrections)
through _process_domain (the production inbound-worker entry to
process_message), not through _extract_datetime in isolation.

Invariants checked on every scripted conversation:
  I1  never book a time the patient did not name or select
  I2  never book a slot that was not offered
  I3  no question repeats unboundedly (a bounded number of identical asks)
  I4  a correction supersedes the earlier reading
"""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
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


async def _seed_split_shift(session: AsyncSession) -> None:
    """Realistic Indian clinic: split shift 09:30-13:00 and 17:00-20:30,
    closed Sunday. Deliberately mixed AM/PM so bare hours are meaningful."""
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
    for open_t, close_t in (("09:30", "13:00"), ("17:00", "20:30")):
        await session.execute(
            text(
                "INSERT INTO operating_schedules "
                "(business_id, day_of_week, open_time, close_time, is_active) "
                f"SELECT 1, day, TIME '{open_t}', TIME '{close_t}', true "
                "FROM generate_series(1, 6) AS day"
            )
        )
    await session.commit()


def _claimed(event_id: int, body: str) -> ClaimedEvent:
    return ClaimedEvent(
        event_id=event_id,
        business_id=1,
        message_id=f"wamid.adv.{uuid.uuid4().hex[:10]}.{event_id}",
        sender_phone="+919123456789",
        message_type="text",
        message_body=body,
        phone_number_id="phone-1",
        claim_token=uuid.uuid4(),
        claim_version=1,
        attempts=0,
        max_attempts=5,
    )


def _gw():
    from unittest.mock import AsyncMock

    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text="ok")
    return gw


@dataclass
class ConvTrace:
    """What the harness observed across a scripted conversation."""

    responses: list[str] = field(default_factory=list)
    offered_slots_utc: set[datetime] = field(default_factory=set)
    committed_start_utc: datetime | None = None
    conv_id: str | None = None


async def run_script(
    pg_session_factory: async_sessionmaker[AsyncSession],
    utterances: list[str],
) -> ConvTrace:
    """Drive a conversation through the production path and collect a trace.

    Each utterance is one inbound message. After each, the active offer's slots
    (if any) are recorded so I2 can check the committed slot was offered.
    """
    trace = ConvTrace()
    gw = _gw()
    for i, utt in enumerate(utterances):
        async with pg_session_factory() as session:
            resp, _ = await _process_domain(_claimed(i + 1, utt), session, gw)
            await session.commit()
        trace.responses.append(resp)
        conv_id = next(iter(_CONVERSATIONS.keys()))
        trace.conv_id = conv_id
        ctx = _CONVERSATIONS[conv_id]
        offer = ctx.collected_facts.get("_active_offer")
        if isinstance(offer, dict):
            for s in offer["slots"]:
                trace.offered_slots_utc.add(
                    datetime.fromisoformat(s["start_at_utc"]).astimezone(UTC)
                )

    async with pg_session_factory() as verify:
        row = (
            await verify.execute(
                text(
                    "SELECT start_at FROM appointments "
                    "WHERE business_id = 1 AND status = 'confirmed'"
                )
            )
        ).one_or_none()
        if row is not None:
            committed = row[0]
            if committed.tzinfo is None:
                committed = committed.replace(tzinfo=UTC)
            trace.committed_start_utc = committed.astimezone(UTC)
    return trace


def _max_identical_repeats(responses: list[str]) -> int:
    """Longest run of the identical assistant response (the loop signature)."""
    if not responses:
        return 0
    # Normalise trivial variation; the loop we guard against is byte-identical.
    counts = Counter(responses)
    return max(counts.values())


# --- Speech-shaped corpus ---------------------------------------------------
#
# Each case: an id/category, the utterances, and the expected committed local
# time (or None = must book nothing). A slot the patient names/selects must be
# in the split-shift windows (09:30-13:00, 17:00-20:30) to be bookable.
#
# The date is always driven to a known future weekday via "tomorrow"/"naalai"
# so the assertions are deterministic. Where a category is about the TIME half,
# service/resource/phone are supplied in the first utterance in speech shape.

# The lead establishes service + resource. The resource name uses the stored
# form "Dr. Priya" so these cases isolate TIME understanding (the milestone
# focus). The punctuation-in-resource-name gap ("dr priya" not matching
# "Dr. Priya") is a separate speech finding reported alongside this harness.
_LEAD = "i want General Consultation with Dr. Priya"
_PHONE = "reach me on 9123456789"


def _tomorrow_weekday() -> datetime:
    d = datetime.now(KOLKATA) + timedelta(days=1)
    while d.isoweekday() == 7:  # skip Sunday (clinic closed)
        d += timedelta(days=1)
    return d


@dataclass
class Case:
    cid: str
    category: str
    utterances: list[str]
    # expected committed local (h, m) or None for "must book nothing"
    expect_local: tuple[int, int] | None


def _corpus() -> list[Case]:
    cases: list[Case] = []

    # 1. No punctuation, no capitals — exact clinic time, lowercase run-on.
    cases.append(
        Case(
            "nopunct-morning",
            "no_punctuation",
            [f"{_LEAD} tomorrow at 10 30 am {_PHONE}", "yes confirm"],
            (10, 30),
        )
    )
    cases.append(
        Case(
            "nopunct-evening-offer",
            "no_punctuation",
            [f"{_LEAD} tomorrow 5 15 pm {_PHONE}", "5 30 pm", "yes confirm"],
            (17, 30),
        )
    )

    # 2. Disfluencies — repeated words, filler.
    cases.append(
        Case(
            "disfluency",
            "disfluency",
            [f"{_LEAD} tomorrow um at ten thirty am {_PHONE}", "yes confirm"],
            (10, 30),
        )
    )

    # 3. STT artifacts / near-misses — spacing, homophones, trailing period.
    cases.append(
        Case(
            "stt-trailing-period",
            "stt_artifact",
            [f"{_LEAD} tomorrow 5 15 {_PHONE}", "5:30 PM.", "yes confirm"],
            (17, 30),
        )
    )

    # 4. Tamil / Tanglish / English code-mixing in one utterance.
    cases.append(
        Case(
            "codemix-tanglish",
            "code_mixing",
            [f"{_LEAD} naalaikku pathu mani kaalai {_PHONE}", "yes confirm"],
            (10, 0),
        )
    )
    cases.append(
        Case(
            "codemix-evening-word",
            "code_mixing",
            [f"{_LEAD} tomorrow aaru mani {_PHONE}", "maalai", "yes confirm"],
            (18, 0),
        )
    )

    # 5. Utterances split across turns (time first, date later).
    cases.append(
        Case(
            "split-turn",
            "split_turn",
            [f"{_LEAD} at 6 mani {_PHONE}", "naalaikku", "6 pm", "yes confirm"],
            (18, 0),
        )
    )

    # 6. Negations — must NOT book the negated time.
    cases.append(
        Case(
            "negation-no-time",
            "negation",
            [f"{_LEAD} tomorrow {_PHONE}", "not 5 pm", "6 pm", "yes confirm"],
            (18, 0),
        )
    )

    # 7. Mid-conversation correction — "no no, evening".
    cases.append(
        Case(
            "correction-evening",
            "correction",
            [
                f"{_LEAD} tomorrow 10 30 am {_PHONE}",
                "no no make it 6 pm",
                "yes confirm",
            ],
            (18, 0),
        )
    )

    # 8. Vague / unbookable — must book nothing, not guess.
    cases.append(
        Case(
            "vague-time",
            "vague",
            [f"{_LEAD} tomorrow sometime {_PHONE}", "whenever"],
            None,
        )
    )

    return cases


_CORPUS = _corpus()


@pytest.mark.parametrize("case", _CORPUS, ids=[c.cid for c in _CORPUS])
async def test_speech_corpus_invariants(
    pg_session_factory: async_sessionmaker[AsyncSession],
    case: Case,
) -> None:
    async with pg_session_factory() as setup:
        await _seed_split_shift(setup)

    trace = await run_script(pg_session_factory, case.utterances)

    tomorrow = _tomorrow_weekday().date()
    expected_utc = None
    if case.expect_local is not None:
        expected_utc = datetime.combine(
            tomorrow, time(*case.expect_local), tzinfo=KOLKATA
        ).astimezone(UTC)

    # I3: no question repeats unboundedly. The ambiguity/date questions are
    # bounded; a corpus conversation is short, so an identical response more
    # than 3 times is a loop.
    assert _max_identical_repeats(trace.responses) <= 3, (
        f"[{case.cid}] a response repeated unboundedly: {trace.responses}"
    )

    if expected_utc is None:
        # Must book nothing.
        assert trace.committed_start_utc is None, (
            f"[{case.cid}] booked {trace.committed_start_utc} but expected nothing"
        )
        return

    # I1: booked exactly the time the patient named/selected.
    booked_local = (
        trace.committed_start_utc.astimezone(KOLKATA) if trace.committed_start_utc else None
    )
    assert trace.committed_start_utc == expected_utc, (
        f"[{case.cid}] booked {booked_local}, expected {expected_utc.astimezone(KOLKATA)}"
    )

    # I2: the committed slot was one that was offered (or was an exact request
    # that became a single-slot offer — check_and_offer records both).
    assert trace.committed_start_utc in trace.offered_slots_utc, (
        f"[{case.cid}] committed a slot that was never offered: "
        f"{trace.committed_start_utc.astimezone(KOLKATA)} not in "
        f"{sorted(s.astimezone(KOLKATA).strftime('%H:%M') for s in trace.offered_slots_utc)}"
    )
