"""Tenant-scoped read tools for conversation orchestration."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.schema import (
    Appointment,
    Business,
    OperatingSchedule,
    Resource,
    ScheduleException,
    Service,
    ServiceResourceEligibility,
)


@dataclass(frozen=True)
class ServiceInfo:
    id: int
    name: str
    duration_minutes: int
    buffer_before_minutes: int
    buffer_after_minutes: int
    price: str | None


@dataclass(frozen=True)
class ResourceInfo:
    id: int
    name: str
    resource_type: str


@dataclass(frozen=True)
class BusinessContext:
    business_id: int
    name: str
    timezone: str
    services: list[ServiceInfo]
    resources: list[ResourceInfo]
    eligibility: list[tuple[int, int]]


@dataclass(frozen=True)
class AvailableSlot:
    start_at: datetime
    end_at: datetime
    resource_id: int
    resource_name: str


async def get_business_context(
    business_id: int,
    session: AsyncSession,
) -> BusinessContext | None:
    business = (
        await session.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()
    if business is None:
        return None

    services_result = await session.execute(
        select(Service).where(
            Service.business_id == business_id,
            Service.is_active.is_(True),
        )
    )
    services = [
        ServiceInfo(
            id=s.id,
            name=s.name,
            duration_minutes=s.duration_minutes,
            buffer_before_minutes=s.buffer_before_minutes,
            buffer_after_minutes=s.buffer_after_minutes,
            price=str(s.price) if s.price is not None else None,
        )
        for s in services_result.scalars()
    ]

    resources_result = await session.execute(
        select(Resource).where(
            Resource.business_id == business_id,
            Resource.is_active.is_(True),
        )
    )
    resources = [
        ResourceInfo(id=r.id, name=r.name, resource_type=r.resource_type)
        for r in resources_result.scalars()
    ]

    eligibility_result = await session.execute(
        select(ServiceResourceEligibility).where(
            ServiceResourceEligibility.business_id == business_id,
            ServiceResourceEligibility.is_active.is_(True),
        )
    )
    eligibility = [(e.service_id, e.resource_id) for e in eligibility_result.scalars()]

    return BusinessContext(
        business_id=business.id,
        name=business.name,
        timezone=business.timezone,
        services=services,
        resources=resources,
        eligibility=eligibility,
    )


async def check_availability(
    business_id: int,
    service_id: int,
    resource_id: int,
    target_date: date,
    session: AsyncSession,
    *,
    duration_minutes: int = 30,
    buffer_before: int = 0,
    buffer_after: int = 0,
    slot_interval: int = 15,
) -> list[AvailableSlot]:
    from fonely.domain.appointments.availability import (
        LocalShift,
        ScheduleExceptionRule,
        TimeWindow,
        derive_windows,
        shifts_for_date,
    )
    from fonely.domain.appointments.availability import (
        overlaps as domain_overlaps,
    )

    resource = (
        await session.execute(
            select(Resource).where(
                Resource.business_id == business_id,
                Resource.id == resource_id,
                Resource.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if resource is None:
        return []

    business = (
        await session.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()
    if business is None:
        return []

    day_of_week = target_date.isoweekday()
    schedules = (
        (
            await session.execute(
                select(OperatingSchedule).where(
                    OperatingSchedule.business_id == business_id,
                    OperatingSchedule.day_of_week == day_of_week,
                    OperatingSchedule.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )

    business_weekly = tuple(
        LocalShift(s.open_time, s.close_time) for s in schedules if s.resource_id is None
    )
    resource_weekly = tuple(
        LocalShift(s.open_time, s.close_time) for s in schedules if s.resource_id == resource_id
    )

    exceptions = (
        (
            await session.execute(
                select(ScheduleException).where(
                    ScheduleException.business_id == business_id,
                    ScheduleException.exception_date == target_date,
                )
            )
        )
        .scalars()
        .all()
    )

    business_exc = None
    resource_exc = None
    for exc in exceptions:
        rule = ScheduleExceptionRule(
            is_closed=exc.is_closed,
            open_time=getattr(exc, "open_time", None),
            close_time=getattr(exc, "close_time", None),
        )
        if exc.resource_id is None:
            business_exc = rule
        elif exc.resource_id == resource_id:
            resource_exc = rule

    shift_windows = shifts_for_date(
        local_day=target_date,
        timezone=business.timezone,
        business_weekly=business_weekly,
        resource_weekly=resource_weekly,
        business_exception=business_exc,
        resource_exception=resource_exc,
    )

    if not shift_windows:
        return []

    existing_appointments = (
        (
            await session.execute(
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

    booked: list[TimeWindow] = []
    for appt in existing_appointments:
        eff_start = appt.effective_start_at or appt.start_at
        eff_end = appt.effective_end_at or appt.end_at
        if eff_start.date() == target_date or eff_end.date() == target_date:
            booked.append(TimeWindow(eff_start, eff_end))

    slots: list[AvailableSlot] = []
    for window in shift_windows:
        slot_start = window.start_at + timedelta(minutes=buffer_before)
        while True:
            appt_window, effective = derive_windows(
                slot_start,
                duration_minutes=duration_minutes,
                buffer_before_minutes=buffer_before,
                buffer_after_minutes=buffer_after,
            )

            if effective.end_at > window.end_at:
                break

            conflict = any(domain_overlaps(effective, b) for b in booked)
            if not conflict:
                slots.append(
                    AvailableSlot(
                        start_at=appt_window.start_at,
                        end_at=appt_window.end_at,
                        resource_id=resource_id,
                        resource_name=resource.name,
                    )
                )

            slot_start += timedelta(minutes=slot_interval)

    return slots


async def validate_slot_time(
    business_id: int,
    resource_id: int | None,
    target_datetime: datetime,
    session: AsyncSession,
) -> tuple[bool, str]:
    day_of_week = target_datetime.isoweekday()

    exceptions = (
        (
            await session.execute(
                select(ScheduleException).where(
                    ScheduleException.business_id == business_id,
                    ScheduleException.exception_date == target_datetime.date(),
                    (
                        (ScheduleException.resource_id == resource_id)
                        | (ScheduleException.resource_id.is_(None))
                    )
                    if resource_id
                    else (ScheduleException.resource_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
    )

    for exc in exceptions:
        if exc.is_closed:
            return False, "Clinic is closed on this date"

    resource_filter = (
        ((OperatingSchedule.resource_id == resource_id) | (OperatingSchedule.resource_id.is_(None)))
        if resource_id
        else (OperatingSchedule.resource_id.is_(None))
    )

    schedules = (
        (
            await session.execute(
                select(OperatingSchedule).where(
                    OperatingSchedule.business_id == business_id,
                    OperatingSchedule.day_of_week == day_of_week,
                    OperatingSchedule.is_active.is_(True),
                    resource_filter,
                )
            )
        )
        .scalars()
        .all()
    )

    if not schedules:
        return False, "No operating schedule for this day"

    target_time = target_datetime.time()
    for schedule in schedules:
        if schedule.open_time <= target_time < schedule.close_time:
            return True, "Within schedule"

    return False, "Outside operating hours"


def format_confirmation_summary(
    service_name: str,
    resource_name: str,
    start_at: datetime,
    price: str | None,
    timezone: str,
) -> str:
    from zoneinfo import ZoneInfo

    local_time = start_at.astimezone(ZoneInfo(timezone))
    day = local_time.strftime("%A %b %d")
    time_str = local_time.strftime("%-I:%M %p")
    summary = f"{service_name} with {resource_name}, {day} at {time_str}"
    if price:
        summary += f", fee ₹{price}"
    return summary


@dataclass(frozen=True)
class PatientAppointment:
    appointment_id: int
    service_name: str
    resource_name: str
    start_at: datetime
    price: str | None
    status: str
    pending_action_id: int
    version: int
    service_id: int
    resource_id: int


async def get_patient_appointments(
    business_id: int,
    customer_phone: str,
    session: AsyncSession,
    *,
    status: str = "confirmed",
    future_only: bool = True,
) -> list[PatientAppointment]:
    from fonely.core.validators import utcnow

    stmt = (
        select(Appointment)
        .where(
            Appointment.business_id == business_id,
            Appointment.customer_phone == customer_phone,
            Appointment.status == status,
        )
        .order_by(Appointment.start_at)
    )
    if future_only:
        stmt = stmt.where(Appointment.start_at > utcnow())
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        PatientAppointment(
            appointment_id=a.id,
            service_name=a.service_name_snapshot,
            resource_name=a.resource_name_snapshot,
            start_at=a.start_at,
            price=str(a.price_snapshot) if a.price_snapshot is not None else None,
            status=a.status,
            pending_action_id=a.pending_action_id,  # type: ignore[arg-type]
            version=a.version,
            service_id=a.service_id,
            resource_id=a.resource_id,
        )
        for a in rows
    ]


def format_appointment_list(appointments: list[PatientAppointment], timezone: str) -> str:
    from zoneinfo import ZoneInfo

    lines: list[str] = []
    for i, appt in enumerate(appointments, 1):
        local = appt.start_at.astimezone(ZoneInfo(timezone))
        day = local.strftime("%A %b %d")
        time_str = local.strftime("%-I:%M %p")
        lines.append(f"{i}. {day} {time_str} — {appt.service_name}, {appt.resource_name}")
    return "\n".join(lines)


def parse_appointment_selection(
    message: str,
    appointments: list[PatientAppointment],
) -> PatientAppointment | None:
    import re

    stripped = message.strip()

    num_match = re.match(r"^(\d+)$", stripped)
    if num_match:
        idx = int(num_match.group(1)) - 1
        if 0 <= idx < len(appointments):
            return appointments[idx]
        return None

    lower = stripped.lower()
    for appt in appointments:
        if appt.service_name.lower() in lower:
            return appt
        if appt.resource_name.lower() in lower:
            return appt
        name_parts = appt.resource_name.lower().split()
        for part in name_parts:
            if len(part) > 2 and part in lower:
                return appt

    return None
