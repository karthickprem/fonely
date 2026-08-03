"""Unified availability service — single source of truth for scheduling."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.appointments.availability import (
    LocalShift,
    ScheduleExceptionRule,
    TimeWindow,
    contains,
    derive_windows,
    overlaps,
    shifts_for_date,
)
from fonely.models.schema import (
    Appointment,
    Business,
    OperatingSchedule,
    Resource,
    ScheduleException,
)
from fonely.services.conversation_tools import AvailableSlot


class AvailabilityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_available_slots(
        self,
        business_id: int,
        resource_id: int,
        target_date: date,
        *,
        service_duration_minutes: int,
        buffer_before: int = 0,
        buffer_after: int = 0,
        slot_interval: int = 15,
    ) -> list[AvailableSlot]:
        biz = await self._get_business(business_id)
        if biz is None:
            return []
        timezone = biz.timezone

        resource = await self._get_resource(business_id, resource_id)
        if resource is None:
            return []

        shift_windows = await self._get_shift_windows(
            business_id, resource_id, target_date, timezone
        )
        if not shift_windows:
            return []

        booked = await self._get_booked_windows(business_id, resource_id, target_date, timezone)

        total_effective = service_duration_minutes + buffer_before + buffer_after
        slots: list[AvailableSlot] = []

        for window in shift_windows:
            current = window.start_at
            while current + timedelta(minutes=total_effective) <= window.end_at:
                slot_start = current + timedelta(minutes=buffer_before)
                slot_end = slot_start + timedelta(minutes=service_duration_minutes)
                effective = TimeWindow(current, slot_end + timedelta(minutes=buffer_after))

                conflict = any(overlaps(effective, booked_win) for booked_win in booked)
                if not conflict:
                    slots.append(
                        AvailableSlot(
                            start_at=slot_start,
                            end_at=slot_end,
                            resource_id=resource_id,
                            resource_name=resource.name,
                        )
                    )
                current += timedelta(minutes=slot_interval)

        return slots

    async def is_slot_available(
        self,
        business_id: int,
        resource_id: int,
        start_at: datetime,
        duration_minutes: int,
        buffer_before: int = 0,
        buffer_after: int = 0,
    ) -> tuple[bool, str]:
        biz = await self._get_business(business_id)
        if biz is None:
            return False, "Business not found"
        timezone = biz.timezone

        tz = ZoneInfo(timezone)
        local_dt = start_at.astimezone(tz)
        target_date = local_dt.date()

        shift_windows = await self._get_shift_windows(
            business_id, resource_id, target_date, timezone
        )
        if not shift_windows:
            return False, "No operating hours for this date"

        _, effective = derive_windows(
            start_at,
            duration_minutes=duration_minutes,
            buffer_before_minutes=buffer_before,
            buffer_after_minutes=buffer_after,
        )

        fits = any(contains(shift, effective) for shift in shift_windows)
        if not fits:
            return False, "Outside operating hours"

        booked = await self._get_booked_windows(business_id, resource_id, target_date, timezone)
        conflict = any(overlaps(effective, booked_win) for booked_win in booked)
        if conflict:
            return False, "Time slot conflicts with existing appointment"

        return True, "Available"

    async def _get_shift_windows(
        self,
        business_id: int,
        resource_id: int,
        target_date: date,
        timezone: str,
    ) -> list[TimeWindow]:
        day_of_week = target_date.isoweekday()

        biz_schedules = (
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
        res_schedules = (
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

        biz_weekly = tuple(LocalShift(s.open_time, s.close_time) for s in biz_schedules)
        res_weekly = tuple(LocalShift(s.open_time, s.close_time) for s in res_schedules)

        biz_exc = await self._get_exception(business_id, None, target_date)
        res_exc = await self._get_exception(business_id, resource_id, target_date)

        windows = shifts_for_date(
            local_day=target_date,
            timezone=timezone,
            business_weekly=biz_weekly,
            resource_weekly=res_weekly,
            business_exception=biz_exc,
            resource_exception=res_exc,
        )
        return list(windows)

    async def _get_exception(
        self, business_id: int, resource_id: int | None, target_date: date
    ) -> ScheduleExceptionRule | None:
        where_clause = [
            ScheduleException.business_id == business_id,
            ScheduleException.exception_date == target_date,
        ]
        if resource_id is None:
            where_clause.append(ScheduleException.resource_id.is_(None))
        else:
            where_clause.append(ScheduleException.resource_id == resource_id)

        exc = (
            await self._session.execute(select(ScheduleException).where(*where_clause))
        ).scalar_one_or_none()

        if exc is None:
            return None
        return ScheduleExceptionRule(
            is_closed=exc.is_closed,
            open_time=exc.open_time,
            close_time=exc.close_time,
        )

    async def _get_booked_windows(
        self,
        business_id: int,
        resource_id: int,
        target_date: date,
        timezone: str,
    ) -> list[TimeWindow]:
        tz = ZoneInfo(timezone)
        appointments = (
            (
                await self._session.execute(
                    select(Appointment).where(
                        Appointment.business_id == business_id,
                        Appointment.resource_id == resource_id,
                        Appointment.status.in_(["confirmed", "completed", "no_show"]),
                    )
                )
            )
            .scalars()
            .all()
        )

        windows: list[TimeWindow] = []
        for appt in appointments:
            eff_start = appt.effective_start_at or appt.start_at
            eff_end = appt.effective_end_at or appt.end_at
            if eff_start.astimezone(tz).date() == target_date:
                windows.append(TimeWindow(eff_start, eff_end))
        return windows

    async def _get_business(self, business_id: int) -> Business | None:
        return (
            await self._session.execute(select(Business).where(Business.id == business_id))
        ).scalar_one_or_none()

    async def _get_resource(self, business_id: int, resource_id: int) -> Resource | None:
        return (
            await self._session.execute(
                select(Resource).where(
                    Resource.business_id == business_id,
                    Resource.id == resource_id,
                    Resource.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
