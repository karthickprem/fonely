"""Authoritative tenant-scoped scheduling policy and capacity reads."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.validators import utcnow
from fonely.domain.appointments.availability import (
    LocalShift,
    ScheduleExceptionRule,
    TimeWindow,
    derive_windows,
    fits_one_shift,
    local_day_utc_bounds,
    overlaps,
    schedule_weekday,
    shifts_for_date,
)
from fonely.domain.appointments.datetimes import add_elapsed, instant, require_aware
from fonely.models.schema import (
    Business,
    OperatingSchedule,
    Resource,
    ScheduleException,
    Service,
    ServiceResourceEligibility,
)
from fonely.repositories.appointments import AppointmentRepository


@dataclass(frozen=True, slots=True)
class AvailableSlot:
    start_at: datetime
    end_at: datetime
    resource_id: int
    resource_name: str


class AvailabilityReason(StrEnum):
    AVAILABLE = "available"
    BUSINESS_NOT_FOUND = "business_not_found"
    SERVICE_NOT_FOUND = "service_not_found"
    RESOURCE_NOT_FOUND = "resource_not_found"
    RESOURCE_INELIGIBLE = "resource_ineligible"
    OUTSIDE_BOOKING_HORIZON = "outside_booking_horizon"
    INSUFFICIENT_NOTICE = "insufficient_notice"
    NO_OPERATING_HOURS = "no_operating_hours"
    OFF_GRID = "off_grid"
    OUTSIDE_OPERATING_HOURS = "outside_operating_hours"
    CAPACITY_CONFLICT = "capacity_conflict"


@dataclass(frozen=True, slots=True)
class AvailabilityDecision:
    available: bool
    reason: AvailabilityReason
    alternatives: tuple[AvailableSlot, ...] = ()


@dataclass(frozen=True, slots=True)
class _SchedulingContext:
    business: Business
    service: Service
    resource: Resource


class AvailabilityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._appointments = AppointmentRepository(session)

    async def get_available_slots(
        self,
        business_id: int,
        service_id: int,
        resource_id: int,
        target_date: date,
        *,
        now: datetime | None = None,
        exclude_appointment_id: int | None = None,
        exclude_allocation_id: int | None = None,
    ) -> list[AvailableSlot]:
        context, _ = await self._load_context(business_id, service_id, resource_id)
        if context is None:
            return []

        checked_now = self._checked_now(now)
        if not self._date_within_horizon(context.business, target_date, checked_now):
            return []

        shift_windows = await self._get_shift_windows(
            business_id,
            resource_id,
            target_date,
            context.business.timezone,
        )
        if not shift_windows:
            return []

        day_start, day_end = local_day_utc_bounds(target_date, context.business.timezone)
        occupied = await self._get_occupied_windows(
            business_id,
            resource_id,
            day_start,
            day_end,
            exclude_appointment_id=exclude_appointment_id,
            exclude_allocation_id=exclude_allocation_id,
        )

        minimum_start = add_elapsed(
            checked_now,
            timedelta(minutes=context.business.appointment_minimum_notice_minutes),
        )
        slots_by_start: dict[datetime, AvailableSlot] = {}
        for shift in shift_windows:
            slot_start = shift.start_at
            while True:
                appointment, effective = derive_windows(
                    slot_start,
                    duration_minutes=context.service.duration_minutes,
                    buffer_before_minutes=context.service.buffer_before_minutes,
                    buffer_after_minutes=context.service.buffer_after_minutes,
                )
                if instant(effective.end_at) > instant(shift.end_at):
                    break
                if (
                    fits_one_shift(effective, (shift,))
                    and instant(slot_start) > instant(checked_now)
                    and instant(slot_start) >= instant(minimum_start)
                    and not any(overlaps(effective, blocked) for blocked in occupied)
                ):
                    slots_by_start.setdefault(
                        instant(appointment.start_at),
                        AvailableSlot(
                            start_at=appointment.start_at,
                            end_at=appointment.end_at,
                            resource_id=resource_id,
                            resource_name=context.resource.name,
                        ),
                    )
                slot_start = add_elapsed(
                    slot_start,
                    timedelta(minutes=context.business.appointment_slot_interval_minutes),
                )

        return sorted(slots_by_start.values(), key=lambda slot: instant(slot.start_at))

    async def check_exact_slot(
        self,
        business_id: int,
        service_id: int,
        resource_id: int,
        start_at: datetime,
        *,
        now: datetime | None = None,
        exclude_appointment_id: int | None = None,
        exclude_allocation_id: int | None = None,
        alternative_limit: int = 3,
    ) -> AvailabilityDecision:
        require_aware(start_at, label="Appointment start")
        context, context_reason = await self._load_context(business_id, service_id, resource_id)
        if context is None:
            assert context_reason is not None
            return AvailabilityDecision(False, context_reason)

        checked_now = self._checked_now(now)
        local_start = start_at.astimezone(ZoneInfo(context.business.timezone))
        target_date = local_start.date()
        if not self._date_within_horizon(context.business, target_date, checked_now):
            return AvailabilityDecision(False, AvailabilityReason.OUTSIDE_BOOKING_HORIZON)

        minimum_start = add_elapsed(
            checked_now,
            timedelta(minutes=context.business.appointment_minimum_notice_minutes),
        )
        if instant(start_at) <= instant(checked_now) or instant(start_at) < instant(minimum_start):
            return AvailabilityDecision(False, AvailabilityReason.INSUFFICIENT_NOTICE)

        shift_windows = await self._get_shift_windows(
            business_id,
            resource_id,
            target_date,
            context.business.timezone,
        )
        if not shift_windows:
            return AvailabilityDecision(False, AvailabilityReason.NO_OPERATING_HOURS)

        slots = await self.get_available_slots(
            business_id,
            service_id,
            resource_id,
            target_date,
            now=checked_now,
            exclude_appointment_id=exclude_appointment_id,
            exclude_allocation_id=exclude_allocation_id,
        )
        requested = instant(start_at)
        if any(instant(slot.start_at) == requested for slot in slots):
            return AvailabilityDecision(True, AvailabilityReason.AVAILABLE)

        _, effective = derive_windows(
            start_at,
            duration_minutes=context.service.duration_minutes,
            buffer_before_minutes=context.service.buffer_before_minutes,
            buffer_after_minutes=context.service.buffer_after_minutes,
        )
        containing_shift = next(
            (
                shift
                for shift in shift_windows
                if instant(shift.start_at) <= instant(effective.start_at)
                and instant(effective.end_at) <= instant(shift.end_at)
            ),
            None,
        )
        reason = AvailabilityReason.OUTSIDE_OPERATING_HOURS
        if containing_shift is not None:
            elapsed_seconds = (requested - instant(containing_shift.start_at)).total_seconds()
            interval_seconds = context.business.appointment_slot_interval_minutes * 60
            if elapsed_seconds < 0 or elapsed_seconds % interval_seconds != 0:
                reason = AvailabilityReason.OFF_GRID
            else:
                day_start, day_end = local_day_utc_bounds(target_date, context.business.timezone)
                occupied = await self._get_occupied_windows(
                    business_id,
                    resource_id,
                    day_start,
                    day_end,
                    exclude_appointment_id=exclude_appointment_id,
                    exclude_allocation_id=exclude_allocation_id,
                )
                reason = (
                    AvailabilityReason.CAPACITY_CONFLICT
                    if any(overlaps(effective, blocked) for blocked in occupied)
                    else AvailabilityReason.OUTSIDE_OPERATING_HOURS
                )

        alternatives = tuple(
            sorted(
                slots,
                key=lambda slot: (
                    abs((instant(slot.start_at) - requested).total_seconds()),
                    instant(slot.start_at),
                    slot.resource_id,
                ),
            )[:alternative_limit]
        )
        return AvailabilityDecision(False, reason, alternatives)

    async def is_slot_available(
        self,
        business_id: int,
        service_id: int,
        resource_id: int,
        start_at: datetime,
        *,
        now: datetime | None = None,
        exclude_appointment_id: int | None = None,
        exclude_allocation_id: int | None = None,
    ) -> tuple[bool, str]:
        decision = await self.check_exact_slot(
            business_id,
            service_id,
            resource_id,
            start_at,
            now=now,
            exclude_appointment_id=exclude_appointment_id,
            exclude_allocation_id=exclude_allocation_id,
            alternative_limit=0,
        )
        return decision.available, decision.reason.value

    def _checked_now(self, now: datetime | None) -> datetime:
        value = now or utcnow()
        require_aware(value, label="Current time")
        return value

    def _date_within_horizon(self, business: Business, target_date: date, now: datetime) -> bool:
        local_today = now.astimezone(ZoneInfo(business.timezone)).date()
        return (
            local_today
            <= target_date
            <= local_today + timedelta(days=business.appointment_booking_horizon_days)
        )

    async def _load_context(
        self, business_id: int, service_id: int, resource_id: int
    ) -> tuple[_SchedulingContext | None, AvailabilityReason | None]:
        business = (
            await self._session.execute(select(Business).where(Business.id == business_id))
        ).scalar_one_or_none()
        if business is None:
            return None, AvailabilityReason.BUSINESS_NOT_FOUND

        service = (
            await self._session.execute(
                select(Service).where(
                    Service.business_id == business_id,
                    Service.id == service_id,
                    Service.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if service is None:
            return None, AvailabilityReason.SERVICE_NOT_FOUND

        resource = (
            await self._session.execute(
                select(Resource).where(
                    Resource.business_id == business_id,
                    Resource.id == resource_id,
                    Resource.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if resource is None:
            return None, AvailabilityReason.RESOURCE_NOT_FOUND

        eligibility = (
            await self._session.execute(
                select(ServiceResourceEligibility.id).where(
                    ServiceResourceEligibility.business_id == business_id,
                    ServiceResourceEligibility.service_id == service_id,
                    ServiceResourceEligibility.resource_id == resource_id,
                    ServiceResourceEligibility.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if eligibility is None:
            return None, AvailabilityReason.RESOURCE_INELIGIBLE
        return _SchedulingContext(business, service, resource), None

    async def _get_shift_windows(
        self,
        business_id: int,
        resource_id: int,
        target_date: date,
        timezone: str,
    ) -> list[TimeWindow]:
        day_of_week = schedule_weekday(target_date)
        business_schedules = (
            (
                await self._session.execute(
                    select(OperatingSchedule).where(
                        OperatingSchedule.business_id == business_id,
                        OperatingSchedule.resource_id.is_(None),
                        OperatingSchedule.day_of_week == day_of_week,
                        OperatingSchedule.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        resource_schedules = (
            (
                await self._session.execute(
                    select(OperatingSchedule).where(
                        OperatingSchedule.business_id == business_id,
                        OperatingSchedule.resource_id == resource_id,
                        OperatingSchedule.day_of_week == day_of_week,
                        OperatingSchedule.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        business_exception = await self._get_exception(business_id, None, target_date)
        resource_exception = await self._get_exception(business_id, resource_id, target_date)
        return list(
            shifts_for_date(
                local_day=target_date,
                timezone=timezone,
                business_weekly=tuple(
                    LocalShift(schedule.open_time, schedule.close_time)
                    for schedule in business_schedules
                ),
                resource_weekly=tuple(
                    LocalShift(schedule.open_time, schedule.close_time)
                    for schedule in resource_schedules
                ),
                business_exception=business_exception,
                resource_exception=resource_exception,
            )
        )

    async def _get_exception(
        self, business_id: int, resource_id: int | None, target_date: date
    ) -> ScheduleExceptionRule | None:
        conditions = [
            ScheduleException.business_id == business_id,
            ScheduleException.exception_date == target_date,
        ]
        conditions.append(
            ScheduleException.resource_id.is_(None)
            if resource_id is None
            else ScheduleException.resource_id == resource_id
        )
        exception = (
            await self._session.execute(select(ScheduleException).where(*conditions))
        ).scalar_one_or_none()
        if exception is None:
            return None
        return ScheduleExceptionRule(
            is_closed=exception.is_closed,
            open_time=exception.open_time,
            close_time=exception.close_time,
        )

    async def _get_occupied_windows(
        self,
        business_id: int,
        resource_id: int,
        range_start_at: datetime,
        range_end_at: datetime,
        *,
        exclude_appointment_id: int | None,
        exclude_allocation_id: int | None,
    ) -> list[TimeWindow]:
        rows = await self._appointments.list_active_allocation_windows(
            business_id,
            resource_id,
            range_start_at.astimezone(UTC),
            range_end_at.astimezone(UTC),
            exclude_appointment_id=exclude_appointment_id,
            exclude_allocation_id=exclude_allocation_id,
        )
        return [TimeWindow(start_at, end_at) for start_at, end_at in rows]
