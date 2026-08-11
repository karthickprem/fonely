"""Unit tests for bare-time offer selection (the M1-review blocker).

_try_offer_selection resolves a bare (no am/pm) patient time against the
authoritative offer set modulo 12, accepting only a UNIQUE match. Two
candidates are genuinely ambiguous, so it must decline and let the caller ask.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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
    assert set(ambiguous) == {"5:30 AM", "5:30 PM"}


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
