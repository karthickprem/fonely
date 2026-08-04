"""Tenant-scoped read tools for conversation orchestration."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.schema import (
    Appointment,
    Business,
    Resource,
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
