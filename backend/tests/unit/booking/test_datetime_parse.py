"""Unit tests for date/time understanding.

Two invariants under test:
- parse_time_of_day never returns a date; parse_relative_date never a time.
- Neither ever guesses: ambiguous or absent input returns None, so the
  caller asks rather than booking the wrong slot.
"""

from datetime import date, time

import pytest

from fonely.domain.booking.datetime_parse import (
    parse_relative_date,
    parse_time_of_day,
)


class TestBareAndDottedTimes:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("10:30", time(10, 30)),
            ("10.30", time(10, 30)),
            ("10 30", time(10, 30)),
            ("1030", time(10, 30)),
            ("ok 10:30", time(10, 30)),
            ("10:30 please", time(10, 30)),
            ("let's do 10:30 am please", time(10, 30)),
            ("10:30 pm", time(22, 30)),
            ("10 am", time(10, 0)),
            ("2 pm", time(14, 0)),
            ("12 am", time(0, 0)),
            ("12 pm", time(12, 0)),
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
        ],
    )
    def test_word_forms(self, text: str, expected: time) -> None:
        assert parse_time_of_day(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            # Bare hour 9-12 stays morning; only small hours (<=8) lean evening,
            # because an Indian clinic's small-hour appointments are afternoon.
            ("pathu mani", time(10, 0)),  # 10 o'clock -> morning
            ("pathu mani kaalai", time(10, 0)),  # 10 morning (explicit)
            ("aaru mani", time(18, 0)),  # 6 -> evening lean
            ("aaru mani kaalai", time(6, 0)),  # 6 morning (explicit)
            ("aaru mani maalai", time(18, 0)),  # 6 evening (explicit)
            ("pathu arai", time(10, 30)),  # 10:30 morning
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
