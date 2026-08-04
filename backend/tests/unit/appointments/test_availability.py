"""Pure scheduling and resource-capacity tests."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pytest

from fonely.domain.appointments.availability import (
    CandidateSlot,
    LocalShift,
    ResourceCandidate,
    ScheduleExceptionRule,
    TimeWindow,
    derive_windows,
    eligible_resources,
    fits_one_shift,
    order_slots,
    overlaps,
    resolve_local_wall_time,
    shifts_for_date,
)
from fonely.domain.appointments.errors import AppointmentDomainError, AppointmentErrorCode


def aware(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 3, hour, minute, tzinfo=UTC)


def test_half_open_overlap_and_adjacency() -> None:
    first = TimeWindow(aware(10), aware(10, 30))
    overlapping = TimeWindow(aware(10, 15), aware(10, 45))
    adjacent = TimeWindow(aware(10, 30), aware(11))
    assert overlaps(first, overlapping)
    assert not overlaps(first, adjacent)


def test_buffers_extend_effective_capacity() -> None:
    appointment, effective = derive_windows(
        aware(10), duration_minutes=30, buffer_before_minutes=5, buffer_after_minutes=10
    )
    assert appointment == TimeWindow(aware(10), aware(10, 30))
    assert effective == TimeWindow(aware(9, 55), aware(10, 40))


def test_appointment_must_fit_one_split_shift() -> None:
    shifts = (TimeWindow(aware(9), aware(13)), TimeWindow(aware(17), aware(21)))
    assert fits_one_shift(TimeWindow(aware(10), aware(11)), shifts)
    assert not fits_one_shift(TimeWindow(aware(12, 30), aware(17, 30)), shifts)


def test_resource_closed_exception_closes_resource() -> None:
    shifts = shifts_for_date(
        local_day=date(2026, 8, 3),
        timezone="Asia/Kolkata",
        business_weekly=(LocalShift(time(9), time(18)),),
        resource_weekly=(LocalShift(time(10), time(17)),),
        business_exception=ScheduleExceptionRule(False, time(11), time(16)),
        resource_exception=ScheduleExceptionRule(True),
    )
    assert shifts == ()


def test_business_closed_exception_is_absolute() -> None:
    shifts = shifts_for_date(
        local_day=date(2026, 8, 3),
        timezone="Asia/Kolkata",
        business_weekly=(LocalShift(time(9), time(18)),),
        resource_weekly=(LocalShift(time(10), time(17)),),
        business_exception=ScheduleExceptionRule(True),
        resource_exception=ScheduleExceptionRule(False, time(10), time(16)),
    )
    assert shifts == ()


def test_modified_business_and_resource_hours_are_intersected() -> None:
    shifts = shifts_for_date(
        local_day=date(2026, 8, 3),
        timezone="Asia/Kolkata",
        business_weekly=(LocalShift(time(9), time(18)),),
        resource_weekly=(LocalShift(time(8), time(20)),),
        business_exception=ScheduleExceptionRule(False, time(11), time(16)),
        resource_exception=ScheduleExceptionRule(False, time(10), time(17)),
    )
    assert len(shifts) == 1
    assert shifts[0].start_at.hour == 11
    assert shifts[0].end_at.hour == 16


def test_resource_exception_cannot_widen_business_modified_hours() -> None:
    shifts = shifts_for_date(
        local_day=date(2026, 8, 3),
        timezone="Asia/Kolkata",
        business_weekly=(LocalShift(time(9), time(18)),),
        resource_weekly=(LocalShift(time(10), time(17)),),
        business_exception=ScheduleExceptionRule(False, time(11), time(15)),
        resource_exception=ScheduleExceptionRule(False, time(9), time(17)),
    )
    assert len(shifts) == 1
    assert shifts[0].start_at.hour == 11
    assert shifts[0].end_at.hour == 15


def test_resource_weekly_intersects_business_weekly() -> None:
    shifts = shifts_for_date(
        local_day=date(2026, 8, 3),
        timezone="Asia/Kolkata",
        business_weekly=(LocalShift(time(9), time(18)),),
        resource_weekly=(LocalShift(time(8), time(13)), LocalShift(time(17), time(20))),
        business_exception=None,
        resource_exception=None,
    )
    assert len(shifts) == 2
    assert shifts[0].start_at.hour == 9
    assert shifts[0].end_at.hour == 13
    assert shifts[1].start_at.hour == 17
    assert shifts[1].end_at.hour == 18


def test_resource_without_schedule_inherits_business_weekly() -> None:
    shifts = shifts_for_date(
        local_day=date(2026, 8, 3),
        timezone="Asia/Kolkata",
        business_weekly=(LocalShift(time(9), time(18)),),
        resource_weekly=(),
        business_exception=None,
        resource_exception=None,
    )
    assert len(shifts) == 1
    assert shifts[0].start_at.hour == 9
    assert shifts[0].end_at.hour == 18


@pytest.mark.parametrize(
    ("resource_id", "expected_code"),
    [
        (3, AppointmentErrorCode.NOT_FOUND),
        (2, AppointmentErrorCode.RESOURCE_INACTIVE),
        (4, AppointmentErrorCode.RESOURCE_INELIGIBLE),
    ],
)
def test_named_resource_never_silently_substitutes(
    resource_id: int, expected_code: AppointmentErrorCode
) -> None:
    resources = (
        ResourceCandidate(1, "One", True, True),
        ResourceCandidate(2, "Two", False, True),
        ResourceCandidate(4, "Four", True, False),
    )
    with pytest.raises(AppointmentDomainError) as exc:
        eligible_resources(resources, named_resource_id=resource_id)
    assert exc.value.code == expected_code


def test_missing_eligibility_is_configuration_error() -> None:
    with pytest.raises(AppointmentDomainError) as exc:
        eligible_resources((ResourceCandidate(1, "One", True, False),))
    assert exc.value.code == AppointmentErrorCode.CONFIGURATION_ERROR


def test_any_resource_and_slots_use_stable_order() -> None:
    resources = (
        ResourceCandidate(2, "Two", True, True),
        ResourceCandidate(1, "One", True, True),
    )
    assert [item.resource_id for item in eligible_resources(resources)] == [1, 2]
    slots = (
        CandidateSlot(2, "Two", aware(10), aware(11), aware(10), aware(11)),
        CandidateSlot(1, "One", aware(10), aware(11), aware(10), aware(11)),
        CandidateSlot(1, "One", aware(9), aware(10), aware(9), aware(10)),
    )
    assert [(item.start_at, item.resource_id) for item in order_slots(slots)] == [
        (aware(9), 1),
        (aware(10), 1),
        (aware(10), 2),
    ]


def test_kolkata_wall_time_is_unambiguous() -> None:
    value = resolve_local_wall_time(date(2026, 8, 3), time(10), "Asia/Kolkata")
    offset = value.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 19800


@pytest.mark.parametrize(
    "build_schedule",
    [
        lambda: LocalShift(time(9, tzinfo=UTC), time(17)),
        lambda: LocalShift(time(9), time(17, tzinfo=UTC)),
        lambda: ScheduleExceptionRule(False, time(9, tzinfo=UTC), time(17)),
        lambda: ScheduleExceptionRule(False, time(9), time(17, tzinfo=UTC)),
    ],
)
def test_schedule_hours_must_be_naive_local_wall_times(
    build_schedule: Callable[[], object],
) -> None:
    with pytest.raises(ValueError, match="naive local wall times"):
        build_schedule()


def test_resolver_rejects_aware_local_wall_time() -> None:
    with pytest.raises(ValueError, match="naive local wall time"):
        resolve_local_wall_time(date(2026, 8, 3), time(10, tzinfo=UTC), "Asia/Kolkata")


@pytest.mark.parametrize(
    ("day", "wall_time", "message"),
    [
        (date(2026, 3, 8), time(2, 30), "does not exist"),
        (date(2026, 11, 1), time(1, 30), "ambiguous"),
    ],
)
def test_dst_invalid_wall_times_rejected(day: date, wall_time: time, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_local_wall_time(day, wall_time, "America/New_York")


def test_duration_arithmetic_uses_elapsed_instants_across_dst() -> None:
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 3, 8, 1, 45, tzinfo=zone)
    appointment, _ = derive_windows(start, duration_minutes=30)
    assert appointment.end_at == datetime(2026, 3, 8, 3, 15, tzinfo=zone)
    assert appointment.end_at.astimezone(UTC) - appointment.start_at.astimezone(UTC) == timedelta(
        minutes=30
    )


def test_time_window_ordering_uses_instants_not_same_zone_wall_times() -> None:
    zone = ZoneInfo("America/New_York")
    first_fold = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=0)
    second_fold = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
    window = TimeWindow(first_fold, second_fold)
    assert window.end_at.astimezone(UTC) > window.start_at.astimezone(UTC)


@pytest.mark.parametrize(
    "values",
    [
        (0, aware(10), aware(11), aware(10), aware(11)),
        (1, datetime(2026, 8, 3, 10), aware(11), aware(10), aware(11)),
        (1, aware(10), aware(10), aware(10), aware(11)),
        (1, aware(10), aware(11), aware(10, 30), aware(11)),
        (1, aware(10), aware(11), aware(10), aware(10, 30)),
    ],
)
def test_candidate_slot_rejects_malformed_intervals(values: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        CandidateSlot(
            resource_id=values[0],  # type: ignore[arg-type]
            resource_name="One",
            start_at=values[1],  # type: ignore[arg-type]
            end_at=values[2],  # type: ignore[arg-type]
            effective_start_at=values[3],  # type: ignore[arg-type]
            effective_end_at=values[4],  # type: ignore[arg-type]
        )


class InvalidTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None


def test_derive_windows_rejects_invalid_tzinfo() -> None:
    value = datetime(2026, 8, 3, 10, tzinfo=InvalidTimezone())
    with pytest.raises(ValueError, match="timezone-aware"):
        derive_windows(value, duration_minutes=30)
