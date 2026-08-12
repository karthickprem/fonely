"""Regression guard against the Dev3 P0: a bare time (no am/pm) must NOT
re-date itself to today. It must keep the date the caller was offered.

In the WhatsApp engine, "10:30" for a TOMORROW offer fell through to a raw
time parse that defaulted the date to TODAY and booked the wrong day. A Tamil
caller naming a time out loud ("பத்தரை", "ten thirty", "10:30") is the same
input shape and STT hands it over constantly. This test proves the voice path
does not share that defect: date and time are independent fields, a bare time
only touches selected_time, and the commit combines the two — the date is the
one held in state, never one a parser invents.
"""
from __future__ import annotations

from datetime import date, time

from fonely.voice.dialogue import BookingCollection, extract_booking_time
from fonely.voice.context import (
    AvailableSlot, DayAvailability,
)


def _availability(target: date, *times: time) -> DayAvailability:
    from fonely.voice.context import SlotStatus
    slots = tuple(
        AvailableSlot(
            resource_id=1, resource_name="Dr. X",
            start_time=t, end_time=t, service_name="scaling",
            status=SlotStatus.AVAILABLE,
        )
        for t in times
    )
    return DayAvailability(
        business_date=target, day_of_week=target.strftime("%A").lower(),
        is_operating_day=True, is_exception_day=False,
        available_slots=slots,
    )


TOMORROW = date(2026, 8, 13)
TODAY = date(2026, 8, 12)


class TestBareTimeKeepsOfferedDate:
    def test_bare_time_after_tomorrow_offer_keeps_tomorrow(self):
        """Offer tomorrow's slots, caller says bare '10:30' → date stays
        tomorrow, not today."""
        bc = BookingCollection()
        bc.active = True
        # Turn 1: caller picks tomorrow. resolved_date carries the date.
        bc.update("நாளைக்கு", resolved_date=TOMORROW, availability=None)
        assert bc.target_date == TOMORROW

        # Turn 2: caller says bare "10:30", no am/pm. resolved_date is None
        # (a bare time is not a relative-date word), so the date must NOT move.
        avail = _availability(TOMORROW, time(10, 30))
        bc.update("10:30", resolved_date=None, availability=avail)

        assert bc.target_date == TOMORROW, "bare time must not re-date to today"
        assert bc.selected_time == time(10, 30)

    def test_bare_time_alone_cannot_set_date(self):
        """A bare time with no prior date leaves target_date None — the gate
        cannot reach commit, so no wrong-day booking is possible."""
        bc = BookingCollection()
        bc.active = True
        avail = _availability(TODAY, time(10, 30))
        bc.update("10:30", resolved_date=None, availability=avail)

        assert bc.target_date is None
        # required_field still asks for date — commit is unreachable.
        assert bc.required_field == "date"

    def test_resolved_date_change_resets_time(self):
        """Changing the date invalidates a previously selected time, so a
        stale time can't ride along to a new date."""
        bc = BookingCollection()
        bc.active = True
        avail_today = _availability(TODAY, time(10, 30))
        bc.update("இன்னைக்கு", resolved_date=TODAY, availability=avail_today)
        bc.update("10:30", resolved_date=None, availability=avail_today)
        assert bc.selected_time == time(10, 30)

        # Caller changes to tomorrow — the time must clear, not carry.
        bc.update("நாளைக்கு மாத்துங்க", resolved_date=TOMORROW, availability=None)
        assert bc.target_date == TOMORROW
        assert bc.selected_time is None

    def test_extract_booking_time_is_time_only_no_date(self):
        """extract_booking_time returns a time object with NO date component,
        so it structurally cannot carry or default a date."""
        t = extract_booking_time("10:30")
        assert isinstance(t, time)
        # A time has no year/month/day — nothing to default.
        assert not hasattr(t, "year")
