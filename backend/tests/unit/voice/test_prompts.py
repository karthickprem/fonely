"""Tests for production prompt architecture."""

from datetime import UTC, date, time

from fonely.voice.context import AvailableSlot, DayAvailability, TrustedClock
from fonely.voice.prompts import (
    build_greeting,
    build_system_prompt,
    format_availability,
)


def _clock() -> TrustedClock:
    from datetime import datetime

    return TrustedClock(
        now_utc=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, 10),
        day_of_week="monday",
    )


def test_no_hardcoded_slots_in_prompt():
    prompt = build_system_prompt(
        clock=_clock(),
        clinic_name="Test Business",
        clinic_context="",
        availability=None,
        session_mode="demo",
    )
    assert "Tomorrow: 10, 11, 5, 6:30, 7:30" not in prompt
    assert "not connected" in prompt.lower() or "not available" in prompt.lower()


def test_demo_mode_disclosed_upfront():
    prompt = build_system_prompt(
        clock=_clock(),
        clinic_name="Test Clinic",
        clinic_context="",
        availability=None,
        session_mode="demo",
    )
    assert "demo" in prompt.lower()
    assert "cannot" in prompt.lower() or "CANNOT" in prompt


def test_live_mode_allows_booking():
    prompt = build_system_prompt(
        clock=_clock(),
        clinic_name="Test Clinic",
        clinic_context="",
        availability=None,
        session_mode="live",
    )
    assert "can create bookings" in prompt.lower() or "can check availability" in prompt.lower()


def test_availability_with_slots():
    avail = DayAvailability(
        business_date=date(2026, 8, 10),
        day_of_week="monday",
        is_operating_day=True,
        is_exception_day=False,
        operating_hours=((time(10, 0), time(13, 0)),),
        available_slots=(AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "consultation"),),
    )
    text = format_availability(avail)
    assert "Dr. Priya" in text
    assert "10:00" in text


def test_availability_closed_day():
    avail = DayAvailability(
        business_date=date(2026, 8, 10),
        day_of_week="sunday",
        is_operating_day=False,
        is_exception_day=False,
        reason="Sunday closed",
    )
    text = format_availability(avail)
    assert "CLOSED" in text
    assert "Sunday closed" in text


def test_availability_fully_booked():
    avail = DayAvailability(
        business_date=date(2026, 8, 10),
        day_of_week="monday",
        is_operating_day=True,
        is_exception_day=False,
        fully_booked=True,
    )
    text = format_availability(avail)
    assert "FULLY BOOKED" in text


def test_availability_none():
    text = format_availability(None)
    assert "not" in text.lower()


def test_today_in_prompt():
    prompt = build_system_prompt(
        clock=_clock(),
        clinic_name="Test Clinic",
        clinic_context="",
        availability=None,
    )
    assert "August 10, 2026" in prompt
    assert "Monday" in prompt


def test_greeting():
    g = build_greeting("Smile Dental")
    assert "Smile Dental" in g
    assert "Fonely" in g
