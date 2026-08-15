"""#45(a): the voice booking captures the resource (dentist) of the slot the
caller SELECTED, and the commit books THAT resource — not a re-resolved
lowest-id one (the wrong-dentist bug).

The release proof (delivery-readiness re-runs this): two dentists at DIFFERENT
times, caller picks Dr B's unique time -> selected_resource_id is Dr B, never
Dr A / lowest-id. The collision test asserts the honest weaker guarantee: on a
same-start_time collision the captured resource is one that WAS available at
that slot, deterministically — the readback names no dentist, so "which dentist"
is not something the caller specified.
"""

from __future__ import annotations

from datetime import date, time

from fonely.voice.context import AvailableSlot, DayAvailability, SlotStatus
from fonely.voice.dialogue import BookingCollection

TOMORROW = date(2026, 8, 16)


def _availability(*slots: tuple[int, time]) -> DayAvailability:
    """Build availability from (resource_id, start_time) pairs — each an
    AVAILABLE slot for the same service."""
    built = tuple(
        AvailableSlot(
            resource_id=rid,
            resource_name=f"Dr. {rid}",
            start_time=t,
            end_time=t,
            service_name="consultation",
            status=SlotStatus.AVAILABLE,
        )
        for rid, t in slots
    )
    return DayAvailability(
        business_date=TOMORROW,
        day_of_week=TOMORROW.strftime("%A").lower(),
        is_operating_day=True,
        is_exception_day=False,
        available_slots=built,
    )


def _select(bc: BookingCollection, spoken_time: str, avail: DayAvailability) -> None:
    bc.active = True
    bc.target_date = TOMORROW
    bc.update(spoken_time, resolved_date=None, availability=avail)


class TestWrongDentistReleaseProof:
    """Dr A (id 3) and Dr B (id 5) at DIFFERENT times. Caller picks Dr B's
    unique time -> captures Dr B, never Dr A / lowest-id."""

    def test_caller_picks_dr_b_unique_time_captures_dr_b(self):
        avail = _availability((3, time(10, 0)), (5, time(11, 0)))  # A@10, B@11
        bc = BookingCollection()
        _select(bc, "11:00", avail)  # caller picks B's unique time

        assert bc.selected_time == time(11, 0)
        assert bc.selected_resource_id == 5  # Dr B — NOT 3 (lowest-id)

    def test_caller_picks_dr_a_unique_time_captures_dr_a(self):
        # The symmetric case: lowest-id A is captured only when the caller
        # actually picked A's time — proving it's the SELECTION, not a default.
        avail = _availability((3, time(10, 0)), (5, time(11, 0)))
        bc = BookingCollection()
        _select(bc, "10:00", avail)  # caller picks A's unique time

        assert bc.selected_time == time(10, 0)
        assert bc.selected_resource_id == 3  # Dr A because the caller picked 10:00


class TestCollisionHonestGuarantee:
    """Two dentists at the SAME start_time. The caller didn't specify which (the
    readback names no dentist), so the guarantee is: captured resource WAS
    available at that slot, deterministically — not a specific expected dentist."""

    def test_same_time_collision_captures_an_available_resource_deterministically(self):
        avail = _availability((5, time(10, 0)), (3, time(10, 0)))  # both at 10:00
        available_at_10 = {5, 3}

        results = []
        for _ in range(5):
            bc = BookingCollection()
            _select(bc, "10:00", avail)
            results.append(bc.selected_resource_id)

        # Deterministic across runs, and always a resource that WAS free at 10:00.
        assert len(set(results)) == 1  # deterministic
        assert results[0] in available_at_10  # from the VALID set
        # First by the availability's own order (id 5 listed first here) — NOT a
        # global lowest-id sort (which would wrongly pick 3).
        assert results[0] == 5

    def test_collision_never_picks_a_resource_not_free_at_that_time(self):
        # Dr 3 is free at 09:00 only; Dr 5 is free at 10:00. Caller says 10:00.
        # The capture must be 5 — never 3, who is NOT available at 10:00 (the
        # exact failure the old global-lowest-id re-resolution could produce).
        avail = _availability((3, time(9, 0)), (5, time(10, 0)))
        bc = BookingCollection()
        _select(bc, "10:00", avail)
        assert bc.selected_resource_id == 5  # never 3 (not free at 10:00)


class TestCaptureClearsWithTime:
    def test_date_change_clears_both_time_and_resource(self):
        avail = _availability((5, time(11, 0)))
        bc = BookingCollection()
        _select(bc, "11:00", avail)
        assert bc.selected_time == time(11, 0)
        assert bc.selected_resource_id == 5

        # A new date must clear BOTH — a stale resource_id from the old date can
        # never survive to book against a new day's slot.
        bc.update("next day", resolved_date=date(2026, 8, 17), availability=None)
        assert bc.selected_time is None
        assert bc.selected_resource_id is None


class TestMutationGuard:
    """Prove the release proof is not vacuous: if capture were dropped and the
    resource fell back to the availability's FIRST slot regardless of the
    selected time, the wrong-dentist assertion would fail."""

    def test_a_time_agnostic_capture_would_book_the_wrong_dentist(self):
        avail = _availability((3, time(10, 0)), (5, time(11, 0)))
        bc = BookingCollection()
        _select(bc, "11:00", avail)  # caller picked B@11

        # The real capture is time-matched -> 5. A buggy time-agnostic capture
        # (first slot regardless of selected_time) would be 3 — the wrong
        # dentist. This asserts the real behaviour is the time-matched one.
        first_slot_regardless = avail.available_slots[0].resource_id
        assert first_slot_regardless == 3  # the buggy value
        assert bc.selected_resource_id == 5  # the correct, time-matched value
        assert bc.selected_resource_id != first_slot_regardless
