"""Unit tests for date/time understanding.

Two invariants under test:
- parse_time_of_day never returns a date; parse_relative_date never a time.
- Neither ever guesses: ambiguous or absent input returns None, so the
  caller asks rather than booking the wrong slot.
"""

from datetime import date, time, timedelta

import pytest

from fonely.domain.booking.datetime_parse import (
    parse_relative_date,
    parse_time_of_day,
    parse_time_spec,
)


class TestBareAndDottedTimes:
    @pytest.mark.parametrize(
        "text,expected",
        [
            # A bare hour keeps its LITERAL value — the parser never leans it
            # toward clinic hours. Meridiem resolution is the caller's job.
            ("10:30", time(10, 30)),
            ("10.30", time(10, 30)),
            ("10 30", time(10, 30)),
            ("1030", time(10, 30)),
            ("ok 10:30", time(10, 30)),
            ("10:30 please", time(10, 30)),
            ("5:30", time(5, 30)),  # NOT 17:30 — bare, meridiem unknown
            ("5.30", time(5, 30)),
            ("2:00", time(2, 0)),
            # Explicit meridiem is honored.
            ("let's do 10:30 am please", time(10, 30)),
            ("10:30 pm", time(22, 30)),
            ("5:30 pm", time(17, 30)),
            ("10 am", time(10, 0)),
            ("2 pm", time(14, 0)),
            ("12 am", time(0, 0)),
            ("12 pm", time(12, 0)),
            # 24h form is unambiguous.
            ("17:30", time(17, 30)),
        ],
    )
    def test_numeric_forms(self, text: str, expected: time) -> None:
        assert parse_time_of_day(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("half past ten", time(10, 30)),
            ("ten thirty", time(10, 30)),
            ("quarter past ten", time(10, 15)),
            ("ten o'clock", time(10, 0)),
            ("ten am", time(10, 0)),
            # Word and numeric now AGREE: neither leans (secondary item 2).
            ("two thirty", time(2, 30)),
            ("half past five", time(5, 30)),
        ],
    )
    def test_word_forms(self, text: str, expected: time) -> None:
        assert parse_time_of_day(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            # No clinic-hours lean: a bare Tamil hour is literal, exactly like a
            # bare numeric hour. "maalai"/"kaalai" set the meridiem explicitly.
            ("pathu mani", time(10, 0)),  # 10, literal
            ("pathu mani kaalai", time(10, 0)),  # 10 am (explicit)
            ("aaru mani", time(6, 0)),  # 6, literal — NOT 18:00
            ("aaru mani kaalai", time(6, 0)),  # 6 am (explicit)
            ("aaru mani maalai", time(18, 0)),  # 6 pm (explicit evening)
            ("pathu arai", time(10, 30)),  # 10:30, literal
        ],
    )
    def test_tamil_tanglish(self, text: str, expected: time) -> None:
        assert parse_time_of_day(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "yes",
            "ok confirm",
            "the first one",
            "sometime",
            "whenever",
            "morning",  # part of day but no hour
        ],
    )
    def test_absent_returns_none(self, text: str) -> None:
        assert parse_time_of_day(text) is None

    def test_never_returns_a_date(self) -> None:
        # The return type is time; there is structurally no date to leak.
        result = parse_time_of_day("tomorrow at 10:30 am")
        assert isinstance(result, time)


class TestOrdinalOneVsHourOne:
    """The word "one" is both the hour 1 and the ordinal in slot-picking
    phrases ("the evening one"). Task #16 suppresses the ordinal reading, but
    must NOT suppress a genuine "one" hour. This matrix is the exact regression
    the D3-M4 reviewer caught: an over-broad guard turned "one thirty" into None
    while "two thirty" still parsed — an indefensible asymmetry.

    Rule: "one" reads as HOUR 1 on positive evidence — an explicit minute
    ("one thirty"/"one fifteen"/"half past one"), a clock token
    ("one o'clock"/"one pm"), or hour position (not preceded by "the"/a
    part-of-day/ordinal word). It reads as the ORDINAL (None) only in a
    slot-picking phrase ("the evening one", "the first one").
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            # Genuine "one" as an HOUR — must parse (regression cases).
            ("one thirty", time(1, 30)),
            ("one fifteen", time(1, 15)),
            ("one in the afternoon", time(13, 0)),
            ("one o'clock", time(1, 0)),
            ("one pm", time(13, 0)),
            ("one am", time(1, 0)),
            ("half past one", time(1, 30)),
            ("quarter past one", time(1, 15)),
            # A different word-number must be unaffected (the asymmetry check).
            ("two thirty", time(2, 30)),
        ],
    )
    def test_one_as_hour_still_parses(self, text: str, expected: time) -> None:
        assert parse_time_of_day(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "the evening one",
            "the morning one",
            "the afternoon one",
            "the first one",
            "the second one",
            "the last one",
            "the one",
        ],
    )
    def test_ordinal_one_names_no_time(self, text: str) -> None:
        # Slot-picking phrase: names no clock time, must return None so the
        # caller re-asks (or resolves against the offer) rather than booking 1am.
        assert parse_time_of_day(text) is None


class TestMeridiemExplicitness:
    """parse_time_spec reports whether am/pm was stated — the blocker fix."""

    @pytest.mark.parametrize(
        "text,expected_time,explicit",
        [
            ("5:30", time(5, 30), False),  # bare -> not explicit
            ("5:30 pm", time(17, 30), True),  # am/pm stated
            ("5:30 in the evening", time(17, 30), True),  # evening -> pm
            ("10:30 am", time(10, 30), True),
            ("17:30", time(17, 30), True),  # 24h is unambiguous
            ("aaru mani", time(6, 0), False),  # bare Tamil hour
            ("aaru mani maalai", time(18, 0), True),  # evening stated
            ("half past five", time(5, 30), False),
        ],
    )
    def test_meridiem_flag(self, text: str, expected_time: time, explicit: bool) -> None:
        spec = parse_time_spec(text)
        assert spec is not None
        assert spec.time == expected_time
        assert spec.meridiem_explicit is explicit


class TestSecondaryFixes:
    def test_afternoon_words_imply_pm(self) -> None:
        # Secondary item 3: _AFTERNOON is wired into meridiem detection.
        assert parse_time_of_day("3 in the afternoon") == time(15, 0)
        assert parse_time_of_day("afternoon 3") == time(15, 0)
        assert parse_time_of_day("2 in the afternoon") == time(14, 0)

    def test_four_digit_year_is_not_a_time(self) -> None:
        # Secondary item 4: "2026" must not read as 20:26.
        assert parse_time_of_day("2026") is None
        assert parse_time_of_day("book me on 2026") is None
        # A real compact time still parses.
        assert parse_time_of_day("1030") == time(10, 30)


class TestRelativeDate:
    TODAY = date(2026, 8, 12)  # a Wednesday

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("today", date(2026, 8, 12)),
            ("tomorrow", date(2026, 8, 13)),
            ("day after tomorrow", date(2026, 8, 14)),
            ("naalai", date(2026, 8, 13)),
            ("innaiku", date(2026, 8, 12)),
        ],
    )
    def test_relative_words(self, text: str, expected: date) -> None:
        assert parse_relative_date(text, self.TODAY) == expected

    def test_weekday_next_occurrence(self) -> None:
        # Wednesday -> "friday" is 2 days ahead
        assert parse_relative_date("friday", self.TODAY) == date(2026, 8, 14)
        # "velli" (Tanglish Friday)
        assert parse_relative_date("velli", self.TODAY) == date(2026, 8, 14)

    def test_same_weekday_means_next_week(self) -> None:
        # "wednesday" said on a Wednesday means next Wednesday, not today
        assert parse_relative_date("wednesday", self.TODAY) == date(2026, 8, 19)

    def test_day_of_month(self) -> None:
        assert parse_relative_date("on the 15th", self.TODAY) == date(2026, 8, 15)
        # A past day-of-month rolls to next month
        assert parse_relative_date("the 5th", self.TODAY) == date(2026, 9, 5)

    @pytest.mark.parametrize(
        "text",
        ["", "yes", "10:30", "sometime", "at 10 am"],
    )
    def test_absent_returns_none(self, text: str) -> None:
        assert parse_relative_date(text, self.TODAY) is None

    def test_never_reads_the_clock(self) -> None:
        # Same input + same injected today is deterministic regardless of wall time.
        a = parse_relative_date("tomorrow", date(2020, 1, 1))
        assert a == date(2020, 1, 2)


class TestSaturdayAnchoredDivergence:
    """Deterministic proof of the 2026-08-15 (Saturday) CI-red mechanism, and of
    why naming the weekday (not "tomorrow") is the fix.

    A clinic closed on Sunday seeds operating_schedules for day_of_week 1..6
    (schedule_weekday = isoweekday() % 7, so Sunday=0 is unseeded/closed). Tests
    that SPOKE "tomorrow" but EXPECTED a skip-Sunday date diverged only on a
    Saturday: "tomorrow" is the closed Sunday (engine refuses), while the helper
    skipped to Monday. Anchoring today to a fixed Saturday (never the wall clock)
    makes this reproducible and guards against a regression to the "tomorrow"
    pattern.
    """

    # 2026-08-15 is the actual Saturday the CI gate went red on. Fixed, injected,
    # never read from the clock — this test means the same thing on any run day.
    SATURDAY = date(2026, 8, 15)

    def test_saturday_tomorrow_is_the_closed_sunday(self) -> None:
        # (1) Literal "tomorrow" on a Saturday resolves to Sunday — the day the
        # clinic is CLOSED. This is the input that made the availability layer
        # correctly refuse to book, which the old tests mis-expected as a booking.
        # The product behaviour is correct; the test expectation was wrong.
        got = parse_relative_date("tomorrow", self.SATURDAY)
        assert got == date(2026, 8, 16)
        assert got.isoweekday() == 7, "Saturday's 'tomorrow' is Sunday (clinic closed)"

    def test_saturday_named_open_weekday_is_a_seeded_day(self) -> None:
        # (2) The FIX pattern: naming the weekday ("monday") resolves to the next
        # open Monday regardless of what day today is — including a Saturday. This
        # is the day the corrected utterances name, and it equals what the test's
        # own _next_weekday helper computes, so they cannot diverge.
        got = parse_relative_date("monday", self.SATURDAY)
        assert got == date(2026, 8, 17)
        # schedule_weekday = isoweekday() % 7; Monday=1 is within the seeded 1..6.
        assert got.isoweekday() % 7 == 1
        # Tamil weekday name for the same day resolves identically (the Tamil
        # cases in the harness use this instead of "naalaikku").
        assert parse_relative_date("thingal", self.SATURDAY) == got

    @staticmethod
    def _old_skip_sunday_helper(today: date) -> date:
        # The exact OLD buggy helper: tomorrow, skipping Sunday. Reproduced here
        # (not imported — the source was removed by the fix) as the negative
        # control, so this test proves what the OLD helper did vs what the engine
        # did with the word "tomorrow".
        d = today + timedelta(days=1)
        while d.isoweekday() == 7:  # skip Sunday
            d += timedelta(days=1)
        return d

    def test_old_helper_and_literal_tomorrow_diverge_only_on_saturday(self) -> None:
        # THE ROOT CAUSE, precisely: the OLD skip-Sunday helper (what the tests
        # EXPECTED) and literal "tomorrow" (what the engine PARSED) agree on every
        # anchor day EXCEPT Saturday. On a Saturday the helper skips to Monday
        # while "tomorrow" is the closed Sunday — that one-day gap is the whole
        # bug. Proven across a full week deterministically.
        for offset in range(7):
            today = date(2026, 8, 10) + timedelta(days=offset)  # Mon..Sun
            engine_parsed = parse_relative_date("tomorrow", today)
            old_expected = self._old_skip_sunday_helper(today)
            assert engine_parsed is not None
            if today.isoweekday() == 6:  # Saturday
                assert engine_parsed != old_expected, (
                    "the bug: on Saturday the engine parses 'tomorrow'=Sunday but "
                    "the old helper expected Monday"
                )
                assert engine_parsed.isoweekday() == 7  # the closed Sunday
                assert old_expected.isoweekday() == 1  # helper's Monday
            else:
                assert engine_parsed == old_expected, (
                    "off Saturday the two agreed, which is why the bug stayed hidden"
                )

    def test_named_weekday_always_parses_back_to_its_target(self) -> None:
        # THE FIX, precisely: naming a weekday makes the utterance and the
        # expected date ONE value on EVERY anchor day. For every possible "today",
        # parse_relative_date(name) equals a same-rule recomputation of that
        # weekday's next occurrence — so the engine and the test can never diverge,
        # unlike the old "tomorrow"-vs-skip-helper pair which diverged on Saturday.
        for offset in range(7):
            today = date(2026, 8, 10) + timedelta(days=offset)  # Mon..Sun anchor
            for name, dow in (("monday", 0), ("thingal", 0), ("friday", 4)):
                got = parse_relative_date(name, today)
                assert got is not None
                # Same next-occurrence rule the harness helpers use.
                days_ahead = (dow - today.weekday()) % 7 or 7
                assert got == today + timedelta(days=days_ahead), (
                    f"named weekday {name!r} on {today} must parse to its next "
                    "occurrence — the single source of truth the fix relies on"
                )
                assert got > today, "the target is always strictly in the future"
