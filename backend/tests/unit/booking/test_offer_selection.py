"""Unit tests for bare-time offer selection (the M1-review blocker).

_try_offer_selection resolves a bare (no am/pm) patient time against the
authoritative offer set modulo 12, accepting only a UNIQUE match. Two
candidates are genuinely ambiguous, so it must decline and let the caller ask.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fonely.domain.booking.offers import build_offer, serialize_offer
from fonely.domain.conversation.state import ConversationContext
from fonely.services.conversation import ConversationService

KOLKATA = ZoneInfo("Asia/Kolkata")


def _svc() -> ConversationService:
    return ConversationService.__new__(ConversationService)


def _ctx_with_offer(*local_times: tuple[int, int]) -> ConversationContext:
    """Build a ctx whose active offer has slots at the given local (h, m)."""
    day = datetime(2026, 8, 12, tzinfo=KOLKATA).date()
    slots = []
    for h, m in local_times:
        start = datetime.combine(
            day, datetime.min.time().replace(hour=h, minute=m), tzinfo=KOLKATA
        ).astimezone(UTC)
        slots.append({"start_at": start, "end_at": start + timedelta(minutes=30)})
    offer = build_offer(
        business_id=1,
        conversation_id="conv-1",
        service_id=1,
        service_name="Consultation",
        resource_id=1,
        resource_name="Dr. Priya",
        target_date=day.isoformat(),
        available_slots=slots,
        business_timezone="Asia/Kolkata",
    )
    ctx = ConversationContext(conversation_id="conv-1", business_id=1)
    ctx.collected_facts["_active_offer"] = serialize_offer(offer)
    return ctx


def test_bare_evening_time_selects_the_pm_slot() -> None:
    ctx = _ctx_with_offer((17, 0), (17, 30))  # 5:00 PM, 5:30 PM
    assert _svc()._try_offer_selection(ctx, "5:30") is True
    selected = ctx.collected_facts["start_at"]
    assert isinstance(selected, datetime)
    assert selected.astimezone(KOLKATA).hour == 17
    assert selected.astimezone(KOLKATA).minute == 30


def test_bare_morning_time_selects_the_am_slot() -> None:
    ctx = _ctx_with_offer((10, 0), (10, 30))  # 10:00 AM, 10:30 AM
    assert _svc()._try_offer_selection(ctx, "10:30") is True
    assert ctx.collected_facts["start_at"].astimezone(KOLKATA).hour == 10


def test_ambiguous_bare_time_across_two_meridiems_asks() -> None:
    # An offer with BOTH 5:30 AM and 5:30 PM: a bare "5:30" matches two slots
    # mod 12 -> genuinely ambiguous. It consumes the turn (returns True) but
    # does NOT select; it keeps the offer and marks the ambiguity so the caller
    # asks "which one" instead of dropping known context and asking for a date.
    ctx = _ctx_with_offer((5, 30), (17, 30))  # 5:30 AM, 5:30 PM
    assert _svc()._try_offer_selection(ctx, "5:30") is True
    assert "start_at" not in ctx.collected_facts
    assert "_active_offer" in ctx.collected_facts  # offer survives
    ambiguous = ctx.collected_facts.get("_selection_ambiguous")
    assert isinstance(ambiguous, list) and len(ambiguous) == 2
    # Candidates are stored as {display, token} so a later meridiem answer
    # resolves against exactly these two slots (defect 3).
    assert {e["display"] for e in ambiguous} == {"5:30 AM", "5:30 PM"}
    assert all(e.get("token") for e in ambiguous)


def test_three_slot_ambiguity_resolves_to_the_asked_candidate() -> None:
    # Defect 3: a mixed-meridiem offer with THREE slots. The ambiguity is only
    # between 6:00 AM and 6:00 PM; a bare "pm" answer must book 6:00 PM, never
    # the earlier 5:30 PM that merely shares the meridiem.
    ctx = _ctx_with_offer((6, 0), (17, 30), (18, 0))  # 6:00 AM, 5:30 PM, 6:00 PM
    assert _svc()._try_offer_selection(ctx, "6 mani") is True  # ambiguous
    amb = ctx.collected_facts["_selection_ambiguous"]
    assert {e["display"] for e in amb} == {"6:00 AM", "6:00 PM"}
    # "pm" resolves against the 6:00 PM candidate, not 5:30 PM.
    assert _svc()._try_offer_selection(ctx, "pm") is True
    booked = ctx.collected_facts["start_at"].astimezone(KOLKATA)
    assert (booked.hour, booked.minute) == (18, 0), booked


def test_ambiguity_marker_cleared_on_explicit_resolution() -> None:
    ctx = _ctx_with_offer((5, 30), (17, 30))
    _svc()._try_offer_selection(ctx, "5:30")  # sets the marker
    assert "_selection_ambiguous" in ctx.collected_facts
    # Patient resolves with an explicit meridiem -> selects and clears marker.
    assert _svc()._try_offer_selection(ctx, "5:30 pm") is True
    assert "_selection_ambiguous" not in ctx.collected_facts
    assert ctx.collected_facts["start_at"].astimezone(KOLKATA).hour == 17


def test_explicit_meridiem_disambiguates_even_when_two_slots_exist() -> None:
    # Same two-meridiem offer, but the patient says "5:30 pm" -> exact match,
    # not ambiguous.
    ctx = _ctx_with_offer((5, 30), (17, 30))
    assert _svc()._try_offer_selection(ctx, "5:30 pm") is True
    assert ctx.collected_facts["start_at"].astimezone(KOLKATA).hour == 17


def test_ordinal_still_works() -> None:
    ctx = _ctx_with_offer((17, 0), (17, 30))
    assert _svc()._try_offer_selection(ctx, "the second one") is True
    assert ctx.collected_facts["start_at"].astimezone(KOLKATA).minute == 30


class TestBareMeridiemWord:
    """The helper that lets a bare 'pm'/'evening'/'மாலை' resolve an ambiguity."""

    def test_english(self) -> None:
        from fonely.services.conversation import _bare_meridiem_word

        assert _bare_meridiem_word("pm") == "pm"
        assert _bare_meridiem_word("PM") == "pm"
        assert _bare_meridiem_word("evening") == "pm"
        assert _bare_meridiem_word("morning") == "am"
        assert _bare_meridiem_word("am") == "am"

    def test_tamil_tanglish(self) -> None:
        from fonely.services.conversation import _bare_meridiem_word

        assert _bare_meridiem_word("மாலை") == "pm"
        assert _bare_meridiem_word("maalai") == "pm"
        assert _bare_meridiem_word("kaalai") == "am"
        assert _bare_meridiem_word("காலை") == "am"

    def test_word_boundary_no_false_positive(self) -> None:
        from fonely.services.conversation import _bare_meridiem_word

        # 'am' inside 'name'/'exam' must NOT match.
        assert _bare_meridiem_word("my name is Karthik") is None
        assert _bare_meridiem_word("the exam") is None
        # No meridiem word at all.
        assert _bare_meridiem_word("hmm not sure") is None
        # Both present -> ambiguous within the reply -> None (do not guess).
        assert _bare_meridiem_word("morning or evening") is None

    def test_bare_am_only_resolves_standalone(self) -> None:
        # Defect 4: 'am' is also the English verb. It must resolve ONLY as a
        # standalone answer, never from inside a sentence, so we never book a
        # 5:30 AM slot for a patient who said "I am not sure".
        from fonely.services.conversation import _bare_meridiem_word

        assert _bare_meridiem_word("I am not sure") is None
        assert _bare_meridiem_word("I am free then") is None
        # Standalone (optionally with trivial filler) still resolves.
        assert _bare_meridiem_word("am") == "am"
        assert _bare_meridiem_word("ok am") == "am"
        assert _bare_meridiem_word("yes pm") == "pm"
        assert _bare_meridiem_word("a.m.") == "am"
        # A meaning-word inside a sentence still resolves (it is unambiguous).
        assert _bare_meridiem_word("I am ok with evening") == "pm"
        assert _bare_meridiem_word("morning please") == "am"

    def test_pagal_is_not_mapped(self) -> None:
        # 'pagal'/'பகல்' means daytime broadly; mapping it to a half of the day
        # would be a guess we then book. It is deliberately not resolved.
        from fonely.services.conversation import _bare_meridiem_word

        assert _bare_meridiem_word("pagal") is None
        assert _bare_meridiem_word("பகல்") is None

    # CEO #17: lock the punctuation/Tanglish-filler forms a real caller (via STT)
    # actually sends. These already resolve; these assertions keep them resolving.
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("PM.", "pm"),  # highest value: trailing period is ordinary STT output
            ("pm.", "pm"),
            ("am.", "am"),
            ("pm!", "pm"),
            ("pm ,", "pm"),
            ("pm please", "pm"),
            ("pm ah", "pm"),  # Tanglish filler
            ("pm da", "pm"),  # Tanglish filler
            ("am please", "am"),
        ],
    )
    def test_punctuation_and_tanglish_filler_resolve(self, message: str, expected: str) -> None:
        from fonely.services.conversation import _bare_meridiem_word

        assert _bare_meridiem_word(message) == expected, (
            f"{message!r} is a clear meridiem answer a caller sends via STT; "
            f"it must resolve to {expected!r}, not cost a turn and drop to the bound"
        )

    # CEO #17 — THE LOAD-BEARING ASSET. A negation must NEVER resolve: "not pm"
    # resolving would book the patient at PM, the exact meridiem they refused —
    # a silent mis-booking (P0). This set guards against a future edit that
    # widens the filler set (e.g. for a new Tanglish particle) and quietly lets
    # "no"/"not" fall through. If this test ever fails, a negation started
    # resolving: that is a MIS-BOOKING, not a test nit.
    @pytest.mark.parametrize(
        "message",
        [
            "no am",
            "not pm",
            "no pm",
            "not am",
            "don't want pm",
            "no not pm",
            "not in the pm",
            "nope pm",
            "never pm",
            "no. pm",
            "not pm please",
            "i am not sure",  # 'am' as the verb, negated
        ],
    )
    def test_negations_never_resolve(self, message: str) -> None:
        from fonely.services.conversation import _bare_meridiem_word

        result = _bare_meridiem_word(message)
        assert result is None, (
            f"MIS-BOOKING: {message!r} is a NEGATION but resolved to {result!r}. "
            f"The patient refused that meridiem; resolving it books them at the "
            f"time they rejected. A widened filler set has let 'no'/'not' fall "
            f"through — 'no'/'not' must stay OUT of the filler set."
        )
