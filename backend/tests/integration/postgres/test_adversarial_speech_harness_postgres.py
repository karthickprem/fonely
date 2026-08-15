"""D3-M4: adversarial harness driving the REAL conversation path with
speech-shaped input, asserting invariants on EVERY input in the corpus.

Every prior probe against this component — mine and the reviewer's — was
text-shaped and mostly unit-level. The demo is a dentist speaking Tamil into a
phone, so the harness feeds speech-shaped utterances (no punctuation/capitals,
disfluencies, STT artifacts, code-mixing, split turns, negations, corrections)
through _process_domain (the production inbound-worker entry to
process_message), not through _extract_datetime in isolation.

Invariants:
  I1  never book a time the patient did not name or select      (every case)
  I2  never book a slot that was not offered                     (every booking)
  I3  no question repeats unboundedly (bounded identical asks)   (every case)
  I4  a correction supersedes the earlier reading               (correction/
      negation cases: asserted via Case.superseded_local — the committed time
      must never equal the reading the correction overrode)
  I5  the booked doctor is the one named, read from the ROW      (item #19:
      every booking asserts the appointments.resource_id equals the intended
      doctor; ambiguous/unknown spoken names fail closed — book no doctor and
      re-ask, never guess)
  I6  a time/service word that collides with a doctor's name      (item #19
      never selects that doctor                                    regression:
      "aaru mani" must not book Dr. Mani; "General Consultation" must not book
      Dr. General — asserted via Case.forbid_resource_id off the ROW)
  I7  the patient can ANSWER the "which doctor?" question the way  (item #19
      people answer it — bare surname "rao", "rao please", ordinal              rescope-2
      "the second one", numbered "2" — and it resolves + books, row-level. An     + CEO #32:
      unresolvable answer escalates (plain ask -> numbered choice) and then
      TERMINATES: past the ladder the ambiguity state is dropped and the
      conversation ends with a call-the-clinic message, so no question — plain
      OR numbered — repeats without limit. The guard is I3, asserted on every
      case: no response may repeat more than 3 times. The liveness case is run
      LONGER than the ladder so a bound that only swapped questions would breach.)

I1 and I3 run on every case; I2 and I5 run on every case that books; I4 runs on
every case that declares a superseded reading; the I5 fail-closed branch runs on
every case that declares expect_resource_refusal; I7 is exercised by the
disambiguation cases (bare-surname/polite/ordinal book; deadlock-bound proves I3
holds even when an answer never resolves). None of these pass vacuously: a
category that does not apply is simply not in play, not a silent pass.
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
from tests.integration.postgres.conftest import seed_whatsapp_channel

pytestmark = pytest.mark.postgres

KOLKATA = ZoneInfo("Asia/Kolkata")


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
    # WhatsApp channel identity moved from settings into business_whatsapp_channels
    # in 0016; the booking/notification path resolves the sending number from this
    # row, so a seed without it now reproduces a genuinely unconfigured business.
    await seed_whatsapp_channel(session, phone_number_id="phone-1")
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'General Consultation', 30, 0, 0, 500.00, true)"
        )
    )
    # Doctors stored with honorific + capitalisation, all eligible. The roster
    # is deliberately ADVERSARIAL for name matching:
    #  - Priya Kumar / Priya Rao share a first name (ambiguity + resolution);
    #  - Arun is a distinct name (the _LEAD doctor, unambiguous spoken form);
    #  - Mani collides with the Tanglish time word "mani" (o'clock) — "aaru
    #    mani" (6 o'clock) must NOT book Dr. Mani. PERMANENT fixture so a matcher
    #    that treats a time word as a name is caught by 16/16, not just by a
    #    one-off case (D3 item #19 rejection: a roster of well-behaved names
    #    cannot surface a name/vocabulary collision).
    #  - General collides with the service word in "General Consultation" — the
    #    service phrase must NOT book Dr. General.
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Dr. Priya Kumar', 'staff', true), "
            "(2, 1, 'Dr. Priya Rao', 'staff', true), "
            "(3, 1, 'Dr. Arun', 'staff', true), "
            "(4, 1, 'Dr. Mani', 'staff', true), "
            "(5, 1, 'Dr. General', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) VALUES "
            "(1, 1, 1, true), (1, 1, 2, true), (1, 1, 3, true), "
            "(1, 1, 4, true), (1, 1, 5, true)"
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
    committed_resource_id: int | None = None
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
                    "SELECT start_at, resource_id FROM appointments "
                    "WHERE business_id = 1 AND status = 'confirmed'"
                )
            )
        ).one_or_none()
        if row is not None:
            committed = row[0]
            if committed.tzinfo is None:
                committed = committed.replace(tzinfo=UTC)
            trace.committed_start_utc = committed.astimezone(UTC)
            # Read the resource_id straight from the appointments ROW — not the
            # transcript — so the assertion has independent detection power: it
            # fails if the wrong doctor is booked even when the reply text looks
            # right.
            trace.committed_resource_id = row[1]
    return trace


def _max_identical_repeats(responses: list[str]) -> int:
    """Highest TOTAL count of any single identical assistant response.

    Counts total occurrences, not the longest consecutive run — a strictly
    stronger loop signal (an unbounded loop drives both up together, and total
    count also catches a response that recurs non-consecutively), so it can
    never miss a loop that a run-length measure would catch.
    """
    if not responses:
        return 0
    counts = Counter(responses)
    return max(counts.values())


# --- Speech-shaped corpus ---------------------------------------------------
#
# Each case: an id/category, the utterances, and the expected committed local
# time (or None = must book nothing). A slot the patient names/selects must be
# in the split-shift windows (09:30-13:00, 17:00-20:30) to be bookable.
#
# The date is driven to a known-open weekday by NAMING that weekday
# ("monday"/…) in the utterance, and computing the expected instant from the
# SAME target date — never the word "tomorrow".
#
# WHY NOT "tomorrow": the engine's parse_relative_date reads "tomorrow" as the
# literal next calendar day, but the expected date used to be computed by a
# skip-Sunday helper. Those two agree on every weekday EXCEPT Saturday, when
# "tomorrow" is Sunday (clinic closed) while the helper skipped to Monday — so
# on a Saturday CI run every "tomorrow" case booked nothing and failed against a
# Monday expectation (the 2026-08-15 red gate). The fix removes the divergence
# at the source: the spoken date and the expected date are one value. Naming the
# weekday is a parser-supported form (parse_relative_date maps "monday" → its
# next occurrence) and, crucially, it means the same thing to the engine and to
# the test, on any day of the week. See test_datetime_parse for the parser-level
# Saturday-anchored proof that "tomorrow"→closed-Sunday is (correctly) refused.

# The lead establishes service + resource. It now names the doctor in SPOKEN
# form — lowercase, honorific-as-word, no punctuation ("doctor arun" for stored
# "Dr. Arun") — so every time-case also drives the spoken resource-name path end
# to end (item #19), instead of hiding it behind the stored form. "Dr. Arun" is
# the distinct, unambiguous name (resource id=3); "Dr. Priya Kumar"/"Dr. Priya
# Rao" exist to exercise ambiguity in dedicated cases.
_ARUN_ID = 3
_LEAD = "i want General Consultation with doctor arun"
_PHONE = "reach me on 9123456789"

# Monday = weekday() 0 = isoweekday 1 = schedule_weekday 1 (seeded open). The
# utterances name this same day (via _DATE_WORD) so the engine and the test
# agree by construction.
_TARGET_DOW = 0  # Monday
_DATE_WORD = "monday"
# Tamil/Tanglish weekday name for the SAME target day (Monday), so the cases
# that exercise the Tamil date-parsing path keep doing so — naming the weekday
# in Tamil ("thingal") instead of the Tamil word for "tomorrow" ("naalaikku"),
# which had the identical skip-Sunday divergence.
_DATE_WORD_TA = "thingal"


def _target_weekday() -> datetime:
    """The next occurrence of a KNOWN-OPEN weekday (Monday), matching exactly
    what parse_relative_date("monday", today) returns for the engine.

    Monday is always seeded open and is unambiguous. The utterances name this
    same weekday (via _DATE_WORD), so the date the engine parses and the date
    the test expects are derived from ONE computation and cannot diverge on any
    day of the week. Deterministic without touching the clock beyond "now".
    """
    d = datetime.now(KOLKATA)
    # (target_dow - today_dow) % 7, with 0 meaning "said on that day = next
    # week" — identical to parse_relative_date's weekday rule, so the expected
    # date equals the engine's parsed date exactly.
    days_ahead = (_TARGET_DOW - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return d + timedelta(days=days_ahead)


@dataclass
class Case:
    cid: str
    category: str
    utterances: list[str]
    # expected committed local (h, m) or None for "must book nothing"
    expect_local: tuple[int, int] | None
    # I4: for a conversation that CORRECTS or NEGATES an earlier reading, the
    # (h, m) that must be SUPERSEDED — the committed time must never equal it.
    # None for conversations with no correction (I4 does not apply to them).
    superseded_local: tuple[int, int] | None = None
    # I5 (item #19): if this case books, the resource_id the appointments row
    # MUST carry. Defaults to _ARUN_ID because _LEAD names "doctor arun". A case
    # that must book NOTHING leaves expect_local None and this is not checked.
    expect_resource_id: int | None = None
    # I5 negative: a resource-ambiguity/unknown case that must NEVER book a
    # specific resource. When True, the case must book nothing AND the response
    # must ask which/for the doctor rather than silently choosing.
    expect_resource_refusal: bool = False
    # I6 (item #19 regression): a resource_id that must NEVER be booked by this
    # conversation, even if something else books. Guards name/vocabulary
    # collisions — "aaru mani" must never select Dr. Mani, "General
    # Consultation" must never select Dr. General. Checked whenever set,
    # independently of whether the case books at all.
    forbid_resource_id: int | None = None


def _corpus() -> list[Case]:
    cases: list[Case] = []

    # 1. No punctuation, no capitals — exact clinic time, lowercase run-on.
    cases.append(
        Case(
            "nopunct-morning",
            "no_punctuation",
            [f"{_LEAD} {_DATE_WORD} at 10 30 am {_PHONE}", "yes confirm"],
            (10, 30),
        )
    )
    cases.append(
        Case(
            "nopunct-evening-offer",
            "no_punctuation",
            [f"{_LEAD} {_DATE_WORD} 5 15 pm {_PHONE}", "5 30 pm", "yes confirm"],
            (17, 30),
        )
    )

    # 2. Disfluencies — repeated words, filler.
    cases.append(
        Case(
            "disfluency",
            "disfluency",
            [f"{_LEAD} {_DATE_WORD} um at ten thirty am {_PHONE}", "yes confirm"],
            (10, 30),
        )
    )

    # 3. STT artifacts / near-misses — spacing, homophones, trailing period.
    cases.append(
        Case(
            "stt-trailing-period",
            "stt_artifact",
            [f"{_LEAD} {_DATE_WORD} 5 15 {_PHONE}", "5:30 PM.", "yes confirm"],
            (17, 30),
        )
    )

    # 4. Tamil / Tanglish / English code-mixing in one utterance. These say
    # "mani" (o'clock) as a TIME word; the roster has a Dr. Mani, so they double
    # as regression guards — the committed doctor must be Arun (from _LEAD),
    # never Dr. Mani (id=4). forbid_resource_id makes that explicit.
    cases.append(
        Case(
            "codemix-tanglish",
            "code_mixing",
            [f"{_LEAD} {_DATE_WORD_TA} pathu mani kaalai {_PHONE}", "yes confirm"],
            (10, 0),
            forbid_resource_id=4,
        )
    )
    cases.append(
        Case(
            "codemix-evening-word",
            "code_mixing",
            [f"{_LEAD} {_DATE_WORD} aaru mani {_PHONE}", "maalai", "yes confirm"],
            (18, 0),
            forbid_resource_id=4,
        )
    )

    # 5. Utterances split across turns (time first, date later). "6 mani" is a
    # time word; must not book Dr. Mani (id=4).
    cases.append(
        Case(
            "split-turn",
            "split_turn",
            [f"{_LEAD} at 6 mani {_PHONE}", _DATE_WORD_TA, "6 pm", "yes confirm"],
            (18, 0),
            forbid_resource_id=4,
        )
    )

    # 6. Negations — must NOT book the negated time. I4: 5 PM is superseded.
    cases.append(
        Case(
            "negation-no-time",
            "negation",
            [f"{_LEAD} {_DATE_WORD} {_PHONE}", "not 5 pm", "6 pm", "yes confirm"],
            (18, 0),
            superseded_local=(17, 0),
        )
    )

    # 7. Mid-conversation correction — "no no, make it 6 pm". I4: the earlier
    # 10:30 AM reading is superseded by the correction.
    cases.append(
        Case(
            "correction-evening",
            "correction",
            [
                f"{_LEAD} {_DATE_WORD} 10 30 am {_PHONE}",
                "no no make it 6 pm",
                "yes confirm",
            ],
            (18, 0),
            superseded_local=(10, 30),
        )
    )

    # 8. Vague / unbookable — must book nothing, not guess.
    cases.append(
        Case(
            "vague-time",
            "vague",
            [f"{_LEAD} {_DATE_WORD} sometime {_PHONE}", "whenever"],
            None,
        )
    )

    # 9. Ordinal "one" — a slot-picking phrase names no time and must NOT book
    # 1 AM (out of hours anyway). Exercises the Task #16 changed file through
    # the real path: "the evening one" is not a clock reading.
    cases.append(
        Case(
            "ordinal-one-no-time",
            "ordinal_one",
            [f"{_LEAD} {_DATE_WORD} {_PHONE}", "the evening one", "whenever"],
            None,
        )
    )

    # 10. Genuine "one thirty" as an hour — the regression the reviewer caught.
    # 1:30 is out of clinic hours, so it must book NOTHING (refuse), but for the
    # RIGHT reason: it parsed 1:30 and found no slot, not because "one thirty"
    # silently returned None. We assert nothing books; the unit matrix proves
    # the parse. (Kept out-of-hours to avoid depending on a 1:30 slot existing.)
    cases.append(
        Case(
            "one-thirty-out-of-hours",
            "ordinal_one",
            [f"{_LEAD} {_DATE_WORD} one thirty {_PHONE}", "whenever"],
            None,
        )
    )

    # 11. Spoken resource name, reordered honorific — "arun doctor" for stored
    # "Dr. Arun". Must book, and the ROW's resource_id must be Arun (id=3).
    cases.append(
        Case(
            "resource-spoken-reordered",
            "resource_name",
            [
                f"i want General Consultation with arun doctor {_DATE_WORD} 10 30 am {_PHONE}",
                "yes confirm",
            ],
            (10, 30),
            expect_resource_id=_ARUN_ID,
        )
    )

    # 12. Ambiguous spoken name — "dr priya" matches BOTH Dr. Priya Kumar and
    # Dr. Priya Rao. Must FAIL CLOSED: book nothing, ask which doctor. Booking
    # either would be a silent wrong-doctor mis-booking.
    cases.append(
        Case(
            "resource-ambiguous-priya",
            "resource_name",
            [
                f"i want General Consultation with dr priya {_DATE_WORD} 10 30 am {_PHONE}",
                "yes confirm",
            ],
            None,
            expect_resource_refusal=True,
        )
    )

    # 13. Ambiguity RESOLVED by a fuller spoken name — "priya rao" out-scores
    # the partial match and books Dr. Priya Rao (id=2), row-level.
    cases.append(
        Case(
            "resource-ambiguity-resolved",
            "resource_name",
            [
                f"i want General Consultation with dr priya {_DATE_WORD} 10 30 am {_PHONE}",
                "priya rao",
                "yes confirm",
            ],
            (10, 30),
            expect_resource_id=2,
        )
    )

    # 14. Unknown spoken name — no active resource named "dr smith". Must refuse
    # and re-ask; never fall through to "any available".
    cases.append(
        Case(
            "resource-unknown-name",
            "resource_name",
            [
                f"i want General Consultation with dr smith {_DATE_WORD} 10 30 am {_PHONE}",
                "yes confirm",
            ],
            None,
            expect_resource_refusal=True,
        )
    )

    # 15. REGRESSION (item #19 rejection): a Tanglish TIME word that collides
    # with a doctor's name. The patient names NO doctor — they say "aaru mani"
    # (6 o'clock) with Dr. Mani on the roster. The old token-overlap matcher
    # booked Dr. Mani for every such patient (silent wrong-doctor, class 1).
    # "mani" is not adjacent to a title/name so it is NOT naming evidence: no
    # doctor is selected, resource_id stays missing, nothing books, and Dr. Mani
    # (id=4) must NEVER be the committed resource. FAILS on the pre-fix matcher.
    # (No refusal re-ask asserted: with no title present this is "no doctor named
    # yet", handled by the generic missing-fact path, not the unknown-doctor
    # refusal — the load-bearing guard here is forbid_resource_id + book-nothing.)
    cases.append(
        Case(
            "resource-timeword-collision-mani",
            "resource_name",
            [
                f"i want General Consultation {_DATE_WORD} aaru mani {_PHONE}",
                "yes confirm",
            ],
            None,
            forbid_resource_id=4,
        )
    )

    # 16. REGRESSION: a SERVICE word that collides with a doctor's name. "General
    # Consultation" with Dr. General on the roster must select the SERVICE, never
    # book Dr. General (id=5). "general" is not adjacent to a title/name so it is
    # not naming evidence. Nothing books; id=5 must never be committed.
    cases.append(
        Case(
            "resource-serviceword-collision-general",
            "resource_name",
            [
                f"i want General Consultation {_DATE_WORD} 10 30 am {_PHONE}",
                "yes confirm",
            ],
            None,
            forbid_resource_id=5,
        )
    )

    # 17-19. Fold-in of the edge probes the reviewer asked to make permanent.
    # Title-only: "with doctor" names no specific doctor -> fail closed.
    cases.append(
        Case(
            "resource-title-only",
            "resource_name",
            [
                f"i want General Consultation with doctor {_DATE_WORD} 10 30 am {_PHONE}",
                "yes confirm",
            ],
            None,
            expect_resource_refusal=True,
        )
    )
    # Cross-token ambiguity: "dr kumar arun" names tokens of two DIFFERENT
    # doctors (Priya Kumar and Arun) -> ambiguous, fail closed.
    cases.append(
        Case(
            "resource-cross-token-ambiguous",
            "resource_name",
            [
                f"i want General Consultation with dr kumar arun {_DATE_WORD} 10 30 am {_PHONE}",
                "yes confirm",
            ],
            None,
            expect_resource_refusal=True,
        )
    )
    # Empty-ish resource turn: a message with no doctor reference at all must NOT
    # trip the unknown-doctor refusal (distinct from title-only). It simply asks
    # normally; here the doctor never gets named so nothing books, but the reply
    # must NOT be the roster refusal — it should ask for the missing doctor
    # generically. We assert nothing books and no specific resource is chosen.
    cases.append(
        Case(
            "resource-no-mention",
            "resource_name",
            [f"i want General Consultation {_DATE_WORD} 10 30 am {_PHONE}", "whenever"],
            None,
        )
    )

    # 20-24. DISAMBIGUATION ANSWERS (rescope-2). After the agent asks "which
    # doctor?", the patient answers the way people actually answer. Each must
    # RESOLVE to Dr. Priya Rao (id=2) and book, row-level — the deadlock the
    # reviewer found (a bare "rao" discarded as vocabulary, question repeating
    # forever) must not recur. These FAIL before the candidate-set path, pass
    # after.
    for cid, answer in (
        ("disambig-bare-surname", "rao"),
        ("disambig-surname-polite", "rao please"),
        ("disambig-ordinal", "the second one"),
    ):
        cases.append(
            Case(
                cid,
                "disambiguation",
                [
                    f"i want General Consultation with dr priya {_DATE_WORD} 10 30 am {_PHONE}",
                    answer,
                    "yes confirm",
                ],
                (10, 30),
                expect_resource_id=2,
            )
        )

    # 23. Disambiguation answered with a NON-match ("dr smith") must re-ask, not
    # deadlock and not silently pick. Books nothing; the no-repeat-3x invariant
    # (I3, asserted on every case) guards the deadlock.
    cases.append(
        Case(
            "disambig-nonmatch-reask",
            "disambiguation",
            [
                f"i want General Consultation with dr priya {_DATE_WORD} 10 30 am {_PHONE}",
                "dr smith",
                "dr smith",
            ],
            None,
            expect_resource_refusal=True,
        )
    )

    # 24. LIVENESS BOUND (rescope-2 item 4 / CEO #32): repeating an unresolvable
    # answer must TERMINATE, not merely swap one repeating question for another.
    # The length is deliberately LONGER than the escalation ladder (plain, plain,
    # numbered, then terminate) — six unresolvable "priya" answers — so a bound
    # that only swapped the plain question for a forever-repeating NUMBERED one
    # would breach I3 here. (A 4-turn case would only prove the first step; this
    # length is chosen to catch a non-terminating SECOND strategy.) Nothing books
    # because no doctor is ever uniquely chosen, and the conversation ends with a
    # call-the-clinic message rather than looping.
    cases.append(
        Case(
            "disambig-liveness-bound",
            "disambiguation",
            [
                f"i want General Consultation with dr priya {_DATE_WORD} 10 30 am {_PHONE}",
                "priya",
                "priya",
                "priya",
                "priya",
                "priya",
                "priya",
            ],
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

    target_day = _target_weekday().date()
    expected_utc = None
    if case.expect_local is not None:
        expected_utc = datetime.combine(
            target_day, time(*case.expect_local), tzinfo=KOLKATA
        ).astimezone(UTC)

    # I3: no question repeats unboundedly. The ambiguity/date questions are
    # bounded; a corpus conversation is short, so an identical response more
    # than 3 times is a loop.
    assert _max_identical_repeats(trace.responses) <= 3, (
        f"[{case.cid}] a response repeated unboundedly: {trace.responses}"
    )

    # I6 (item #19 regression): a forbidden resource must NEVER be committed —
    # the name/vocabulary-collision guard. Checked first and unconditionally
    # (independent of whether the case books) so a time word "mani" selecting
    # Dr. Mani, or the service word "general" selecting Dr. General, is caught
    # even if some other assertion would also fire. Independent detection power:
    # it reads resource_id off the appointments ROW.
    if case.forbid_resource_id is not None:
        assert trace.committed_resource_id != case.forbid_resource_id, (
            f"[{case.cid}] I6 violated: committed the FORBIDDEN resource_id "
            f"{case.forbid_resource_id} — a time/service word selected a "
            "colliding doctor name (silent wrong-doctor booking)"
        )

    # I4: a correction supersedes the earlier reading. For any conversation that
    # rejects/corrects an earlier time, the committed booking must NEVER equal
    # the superseded reading — whether the correction names a replacement
    # (negation-no-time, correction-evening: must book the NEW time, not the
    # old) or names none (must book nothing). Asserted here, before the general
    # I1/I2 checks, so the invariant runs on every case that declares one.
    if case.superseded_local is not None:
        superseded_utc = datetime.combine(
            target_day, time(*case.superseded_local), tzinfo=KOLKATA
        ).astimezone(UTC)
        assert trace.committed_start_utc != superseded_utc, (
            f"[{case.cid}] I4 violated: booked the SUPERSEDED reading "
            f"{time(*case.superseded_local)}; a correction must override it"
        )

    # I5 negative (item #19): an ambiguous or unknown spoken resource name must
    # fail closed — book NO resource AND ask for the doctor. Two independent
    # checks: (a) the appointments row has no resource_id (nothing booked), and
    # (b) the last reply asks which/for the doctor. Check (b) gives this real
    # detection power: a build that silently picked a doctor would either book
    # (fails a) or reply without asking (fails b), so the assertion cannot pass
    # vacuously the way an I1-shadowed check would.
    if case.expect_resource_refusal:
        assert trace.committed_resource_id is None, (
            f"[{case.cid}] I5 violated: booked resource "
            f"{trace.committed_resource_id} for an ambiguous/unknown spoken name; "
            "must fail closed"
        )
        last = trace.responses[-1].lower() if trace.responses else ""
        assert "doctor" in last or "which" in last, (
            f"[{case.cid}] I5 violated: expected a which-doctor re-ask, got {trace.responses[-1]!r}"
        )
        return

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

    # I5 (item #19): the committed appointment's resource_id — read from the ROW,
    # not the transcript — is the intended doctor. Independent detection power:
    # a wrong-doctor booking fails here even if the reply text reads correctly
    # and the time (I1) is right. Every booking case names a doctor, so this is
    # asserted on all of them; expect_resource_id defaults to _ARUN_ID (_LEAD).
    expected_rid = case.expect_resource_id if case.expect_resource_id is not None else _ARUN_ID
    assert trace.committed_resource_id == expected_rid, (
        f"[{case.cid}] I5 violated: booked resource_id "
        f"{trace.committed_resource_id}, expected {expected_rid}"
    )


# --- Regression: the closed-day divergence, proven end-to-end and deterministically.
# The corpus above proves the HAPPY path (naming an open weekday books) on every
# run day. These two prove the OTHER half the old "tomorrow" bug accidentally
# relied on: a day-relative utterance that lands on a CLOSED day is correctly
# refused — the product behaviour the red gate was actually exercising. Made
# deterministic by seeding the clinic CLOSED on the exact weekday the utterance
# resolves to, so it does not depend on which day CI runs.


async def _seed_clinic_closed_on(session: AsyncSession, closed_dow: int) -> None:
    """The split-shift clinic, but CLOSED on schedule_weekday `closed_dow` — used
    to prove a booking on that day is refused regardless of the run date."""
    await _seed_split_shift(session)
    await session.execute(
        text("DELETE FROM operating_schedules WHERE business_id = 1 AND day_of_week = :d"),
        {"d": closed_dow},
    )
    await session.commit()


async def test_named_open_weekday_books_regardless_of_run_day(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # HAPPY path, isolated and explicit: naming the open weekday (_DATE_WORD)
    # books the intended slot, on any day of the week the test runs. This is the
    # invariant the corpus relies on; asserting it directly guards the fix.
    async with pg_session_factory() as setup:
        await _seed_split_shift(setup)

    trace = await run_script(
        pg_session_factory,
        [f"{_LEAD} {_DATE_WORD} at 10 30 am {_PHONE}", "yes confirm"],
    )
    target = _target_weekday().date()
    expected = datetime.combine(target, time(10, 30), tzinfo=KOLKATA).astimezone(UTC)
    assert trace.committed_start_utc == expected, (
        f"naming the open weekday must book it; booked {trace.committed_start_utc}, "
        f"expected {expected}"
    )


async def test_relative_date_on_a_closed_day_is_refused(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The behaviour the Saturday red gate was actually exercising: a day-relative
    # utterance ("tomorrow") that resolves to a CLOSED day must book NOTHING and
    # offer to try another date — the product is correct to refuse. Deterministic:
    # we compute tomorrow's actual weekday and seed the clinic closed on exactly
    # that day, so the refusal is proven on any run date, not only on Saturdays.
    from zoneinfo import ZoneInfo

    tomorrow = (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=1)).date()
    closed_dow = tomorrow.isoweekday() % 7  # schedule_weekday convention

    async with pg_session_factory() as setup:
        await _seed_clinic_closed_on(setup, closed_dow)

    trace = await run_script(
        pg_session_factory,
        [f"{_LEAD} tomorrow at 10 30 am {_PHONE}", "yes confirm"],
    )
    assert trace.committed_start_utc is None, (
        "a booking on a CLOSED day must be refused, not committed; "
        f"booked {trace.committed_start_utc}"
    )
    joined = " ".join(trace.responses).lower()
    assert "available" in joined or "another" in joined or "which" in joined, (
        f"expected a not-available / try-another-date reply, got {trace.responses}"
    )
