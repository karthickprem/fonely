"""Pure appointment availability and resource-capacity rules."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fonely.domain.appointments.datetimes import add_elapsed, instant, require_aware
from fonely.domain.appointments.errors import AppointmentDomainError, AppointmentErrorCode


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        try:
            start = instant(self.start_at)
            end = instant(self.end_at)
        except ValueError as exc:
            raise ValueError("Time windows require aware datetimes") from exc
        if end <= start:
            raise ValueError("Time window end must be after start")


@dataclass(frozen=True, slots=True)
class LocalShift:
    open_time: time
    close_time: time

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is not None or self.close_time.tzinfo is not None:
            raise ValueError("Shift hours must be naive local wall times")
        if self.close_time <= self.open_time:
            raise ValueError("Shift close must be after open")


@dataclass(frozen=True, slots=True)
class ScheduleExceptionRule:
    is_closed: bool
    open_time: time | None = None
    close_time: time | None = None

    def __post_init__(self) -> None:
        if self.is_closed:
            if self.open_time is not None or self.close_time is not None:
                raise ValueError("Closed exception cannot provide hours")
            return
        if self.open_time is None or self.close_time is None:
            raise ValueError("Modified-hours exception requires a valid interval")
        if self.open_time.tzinfo is not None or self.close_time.tzinfo is not None:
            raise ValueError("Exception hours must be naive local wall times")
        if self.close_time <= self.open_time:
            raise ValueError("Modified-hours exception requires a valid interval")


@dataclass(frozen=True, slots=True)
class ResourceCandidate:
    resource_id: int
    resource_name: str
    active: bool
    eligible: bool


@dataclass(frozen=True, slots=True)
class CandidateSlot:
    resource_id: int
    resource_name: str
    start_at: datetime
    end_at: datetime
    effective_start_at: datetime
    effective_end_at: datetime

    def __post_init__(self) -> None:
        if self.resource_id <= 0:
            raise ValueError("Candidate slot resource ID must be positive")
        start = instant(self.start_at)
        end = instant(self.end_at)
        effective_start = instant(self.effective_start_at)
        effective_end = instant(self.effective_end_at)
        if end <= start:
            raise ValueError("Candidate slot end must be after start")
        if effective_end <= effective_start:
            raise ValueError("Candidate slot effective end must be after effective start")
        if effective_start > start or end > effective_end:
            raise ValueError("Candidate slot effective interval must enclose appointment interval")


def overlaps(left: TimeWindow, right: TimeWindow) -> bool:
    return instant(left.start_at) < instant(right.end_at) and instant(right.start_at) < instant(
        left.end_at
    )


def derive_windows(
    start_at: datetime,
    *,
    duration_minutes: int,
    buffer_before_minutes: int = 0,
    buffer_after_minutes: int = 0,
) -> tuple[TimeWindow, TimeWindow]:
    require_aware(start_at, label="Appointment start")
    if not 1 <= duration_minutes <= 720:
        raise ValueError("Duration must be between 1 and 720 minutes")
    if not 0 <= buffer_before_minutes <= 240:
        raise ValueError("Before buffer must be between 0 and 240 minutes")
    if not 0 <= buffer_after_minutes <= 240:
        raise ValueError("After buffer must be between 0 and 240 minutes")
    appointment = TimeWindow(start_at, add_elapsed(start_at, timedelta(minutes=duration_minutes)))
    effective = TimeWindow(
        add_elapsed(appointment.start_at, -timedelta(minutes=buffer_before_minutes)),
        add_elapsed(appointment.end_at, timedelta(minutes=buffer_after_minutes)),
    )
    return appointment, effective


def contains(outer: TimeWindow, inner: TimeWindow) -> bool:
    return instant(outer.start_at) <= instant(inner.start_at) and instant(inner.end_at) <= instant(
        outer.end_at
    )


def fits_one_shift(window: TimeWindow, shifts: tuple[TimeWindow, ...]) -> bool:
    return any(contains(shift, window) for shift in shifts)


def resolve_local_wall_time(local_day: date, local_time: time, timezone: str) -> datetime:
    """Return one valid aware instant, rejecting DST gaps and ambiguity."""
    if local_time.tzinfo is not None:
        raise ValueError("Local time must be a naive local wall time")
    zone = ZoneInfo(timezone)
    naive = datetime.combine(local_day, local_time)
    valid: dict[datetime, datetime] = {}
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive and round_trip.fold == fold:
            valid[candidate.astimezone(UTC)] = candidate
    if not valid:
        raise ValueError("Local time does not exist in the business timezone")
    if len(valid) > 1:
        raise ValueError("Local time is ambiguous in the business timezone")
    return next(iter(valid.values()))


def _exception_shifts(
    exception: ScheduleExceptionRule,
) -> tuple[LocalShift, ...]:
    if exception.is_closed:
        return ()
    assert exception.open_time is not None
    assert exception.close_time is not None
    return (LocalShift(exception.open_time, exception.close_time),)


def _intersect_local_shifts(
    left: tuple[LocalShift, ...],
    right: tuple[LocalShift, ...],
) -> tuple[LocalShift, ...]:
    intersections = []
    for left_shift in left:
        for right_shift in right:
            open_time = max(left_shift.open_time, right_shift.open_time)
            close_time = min(left_shift.close_time, right_shift.close_time)
            if open_time < close_time:
                intersections.append(LocalShift(open_time, close_time))
    return tuple(sorted(intersections, key=lambda shift: (shift.open_time, shift.close_time)))


def shifts_for_date(
    *,
    local_day: date,
    timezone: str,
    business_weekly: tuple[LocalShift, ...],
    resource_weekly: tuple[LocalShift, ...],
    business_exception: ScheduleExceptionRule | None,
    resource_exception: ScheduleExceptionRule | None,
) -> tuple[TimeWindow, ...]:
    if business_exception is not None and business_exception.is_closed:
        return ()
    if resource_exception is not None and resource_exception.is_closed:
        return ()

    business_effective = (
        _exception_shifts(business_exception) if business_exception is not None else business_weekly
    )
    if business_exception is None and resource_exception is None:
        selected = resource_weekly if resource_weekly else business_weekly
    else:
        resource_effective = (
            _exception_shifts(resource_exception)
            if resource_exception is not None
            else resource_weekly or business_effective
        )
        selected = _intersect_local_shifts(business_effective, resource_effective)

    return tuple(
        TimeWindow(
            resolve_local_wall_time(local_day, shift.open_time, timezone),
            resolve_local_wall_time(local_day, shift.close_time, timezone),
        )
        for shift in selected
    )


def eligible_resources(
    resources: tuple[ResourceCandidate, ...],
    *,
    named_resource_id: int | None = None,
) -> tuple[ResourceCandidate, ...]:
    if named_resource_id is not None:
        named = next(
            (resource for resource in resources if resource.resource_id == named_resource_id), None
        )
        if named is None:
            raise AppointmentDomainError(
                AppointmentErrorCode.NOT_FOUND,
                "The requested resource was not found",
            )
        if not named.active:
            raise AppointmentDomainError(
                AppointmentErrorCode.RESOURCE_INACTIVE,
                "The requested resource is inactive",
            )
        if not named.eligible:
            raise AppointmentDomainError(
                AppointmentErrorCode.RESOURCE_INELIGIBLE,
                "The requested resource is not eligible for this service",
            )
        return (named,)
    eligible = tuple(resource for resource in resources if resource.active and resource.eligible)
    if not eligible:
        raise AppointmentDomainError(
            AppointmentErrorCode.CONFIGURATION_ERROR,
            "No active eligible resource is configured for this service",
        )
    return tuple(sorted(eligible, key=lambda resource: resource.resource_id))


def order_slots(
    slots: tuple[CandidateSlot, ...], limit: int | None = None
) -> tuple[CandidateSlot, ...]:
    ordered = tuple(sorted(slots, key=lambda slot: (instant(slot.start_at), slot.resource_id)))
    return ordered if limit is None else ordered[:limit]


def validate_booking_window(
    start_at: datetime,
    *,
    now: datetime,
    booking_horizon_days: int,
) -> None:
    try:
        start_instant = instant(start_at)
        now_instant = instant(now)
    except ValueError as exc:
        raise ValueError("Booking timestamps must be timezone-aware") from exc
    if start_instant <= now_instant:
        raise ValueError("Appointment must be in the future")
    if booking_horizon_days <= 0:
        raise ValueError("Booking horizon must be positive")
    if start_instant > add_elapsed(now_instant, timedelta(days=booking_horizon_days)):
        raise ValueError("Appointment exceeds the booking horizon")
