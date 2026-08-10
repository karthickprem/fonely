"""Tests for trusted clock, relative date resolution, and availability ports."""
from datetime import date, datetime, time, timezone

import pytest

from fonely.voice.context import (
    AvailableSlot,
    DayAvailability,
    SlotStatus,
    StubAvailabilityPort,
    TrustedClock,
    resolve_relative_date,
)


def _clock(tz: str = "Asia/Kolkata", year: int = 2026, month: int = 8, day: int = 10) -> TrustedClock:
    import zoneinfo
    local = datetime(year, month, day, 14, 30, tzinfo=zoneinfo.ZoneInfo(tz))
    return TrustedClock(
        now_utc=local.astimezone(timezone.utc),
        business_timezone=tz,
        business_date=date(year, month, day),
        day_of_week=local.strftime("%A").lower(),
    )


class TestTrustedClock:
    def test_from_now(self):
        clock = TrustedClock.from_now("Asia/Kolkata")
        assert clock.business_timezone == "Asia/Kolkata"
        assert isinstance(clock.business_date, date)
        assert clock.day_of_week in {
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        }

    def test_immutable(self):
        clock = _clock()
        with pytest.raises(AttributeError):
            clock.business_date = date(2020, 1, 1)


class TestRelativeDateResolution:
    def test_today_tamil(self):
        clock = _clock(day=10)
        assert resolve_relative_date("இன்று doctor free-ஆ?", clock) == date(2026, 8, 10)

    def test_today_tanglish(self):
        clock = _clock(day=10)
        assert resolve_relative_date("innaikku appointment venum", clock) == date(2026, 8, 10)

    def test_today_english(self):
        clock = _clock(day=10)
        assert resolve_relative_date("is the doctor free today?", clock) == date(2026, 8, 10)

    def test_tomorrow_tamil(self):
        clock = _clock(day=10)
        assert resolve_relative_date("நாளைக்கு slot இருக்கா?", clock) == date(2026, 8, 11)

    def test_tomorrow_tanglish(self):
        clock = _clock(day=10)
        assert resolve_relative_date("naalaikku 6:30 available-a?", clock) == date(2026, 8, 11)

    def test_day_after_tomorrow(self):
        clock = _clock(day=10)
        assert resolve_relative_date("day after tomorrow free?", clock) == date(2026, 8, 12)

    def test_no_relative_date(self):
        clock = _clock(day=10)
        assert resolve_relative_date("what services do you offer?", clock) is None

    def test_longer_phrase_wins(self):
        clock = _clock(day=10)
        result = resolve_relative_date("நாளை மறுநாள் appointment", clock)
        assert result == date(2026, 8, 12)

    def test_case_insensitive(self):
        clock = _clock(day=10)
        assert resolve_relative_date("TODAY please", clock) == date(2026, 8, 10)


class TestDayAvailability:
    def test_operating_day_with_slots(self):
        avail = DayAvailability(
            business_date=date(2026, 8, 10),
            day_of_week="monday",
            is_operating_day=True,
            is_exception_day=False,
            operating_hours=((time(10, 0), time(13, 0)), (time(17, 0), time(20, 30))),
            available_slots=(
                AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "consultation"),
                AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),
            ),
        )
        assert avail.is_operating_day
        assert not avail.fully_booked
        assert len(avail.available_slots) == 2

    def test_fully_booked(self):
        avail = DayAvailability(
            business_date=date(2026, 8, 10),
            day_of_week="monday",
            is_operating_day=True,
            is_exception_day=False,
            fully_booked=True,
        )
        assert avail.fully_booked

    def test_closed_day(self):
        avail = DayAvailability(
            business_date=date(2026, 8, 10),
            day_of_week="sunday",
            is_operating_day=False,
            is_exception_day=False,
            reason="Sunday closed",
        )
        assert not avail.is_operating_day

    def test_exception_leave(self):
        avail = DayAvailability(
            business_date=date(2026, 8, 15),
            day_of_week="friday",
            is_operating_day=False,
            is_exception_day=True,
            reason="Independence Day holiday",
        )
        assert avail.is_exception_day
        assert not avail.is_operating_day


class TestStubAvailabilityPort:
    @pytest.mark.asyncio
    async def test_stub_returns_not_connected(self):
        stub = StubAvailabilityPort()
        result = await stub.query_day_availability(1, date(2026, 8, 10))
        assert not result.is_operating_day
        assert "not connected" in result.reason
