"""Voice-facing clinic resolver — turns caller speech into trusted ids.

Layer boundary (see backend_ports.py for the layer below):
  - This module resolves IDENTITY only: a caller says "scaling", this maps
    it to a real service_id for THIS business, or refuses. It also renders
    the system-prompt context text from the database.
  - It never returns price, duration, buffers, or the doctor's spoken name
    as authority. Those come back from the engine in the commit receipt.
  - The single function that commits an appointment (book_appointment) does
    NOT construct AppointmentService itself. It calls the CommandPort. The
    port is the only place a commit is constructed. A test enforces this.

Four invariants, all load-bearing:
  1. Every lookup is tenant-scoped: business_id is in the WHERE clause, never
     filtered after the fact. A resolver that can return another clinic's
     service id is a cross-tenant booking waiting to happen.
  2. Unknown is refused, never approximated. If the caller's phrase does not
     match a real service, resolution returns None and the caller re-asks.
     No fuzzy score decides a booking.
  3. Identity only. service_id/resource_id/business_id in; price/duration/name
     out of the receipt, not out of here.
  4. One commit path, proven structurally (see test_single_commit_path).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("fonely.voice.clinic_resolver")


# Service-name aliases: caller phrasing → canonical name substring. Exact and
# substring matches are tried first; aliases only bridge known synonyms. This
# is NOT fuzzy matching — every entry is a hand-verified equivalence, and a
# phrase matching nothing here still refuses rather than guessing.
_SERVICE_ALIASES: dict[str, tuple[str, ...]] = {
    "scaling": ("cleaning", "polish", "scal", "clean"),
    "cleaning": ("scaling", "polish", "scal"),
    "consultation": ("checkup", "check up", "consult", "review"),
    "checkup": ("consultation", "consult"),
    "extraction": ("remove", "pull", "extract"),
    "filling": ("cavity", "fill"),
    "root canal": ("rct", "canal"),
    "braces": ("braces review", "orthodontic"),
}


@dataclass(frozen=True)
class ResolvedService:
    service_id: int
    name: str


@dataclass(frozen=True)
class ResolvedResource:
    resource_id: int
    name: str


@dataclass(frozen=True)
class ClinicIdentity:
    business_id: int
    name: str
    timezone: str


async def resolve_business(session: AsyncSession, business_id: int) -> ClinicIdentity:
    """Resolve the trusted business identity. business_id comes from the
    application (dialed-number mapping), never from caller payload."""
    row = await session.execute(
        sql_text("SELECT id, name, timezone FROM businesses WHERE id = :id"),
        {"id": business_id},
    )
    r = row.first()
    if r is None:
        raise ValueError(f"business {business_id} not found")
    return ClinicIdentity(business_id=r[0], name=r[1], timezone=r[2])


async def resolve_service(
    session: AsyncSession, business_id: int, service_phrase: str
) -> ResolvedService | None:
    """Map a caller/LLM service phrase to a real service for THIS business.

    Tenant-scoped (invariant 1) and refuse-unknown (invariant 2): returns None
    when nothing matches, so the caller re-asks rather than booking a guess.
    """
    phrase = service_phrase.lower().strip()
    if not phrase:
        return None

    rows = await session.execute(
        sql_text(
            "SELECT id, name FROM services "
            "WHERE business_id = :bid AND is_active = true"
        ),
        {"bid": business_id},
    )
    services = [(r[0], r[1]) for r in rows.fetchall()]

    # 1. Exact / substring match either direction.
    for sid, name in services:
        name_l = name.lower()
        if name_l == phrase or name_l in phrase or phrase in name_l:
            return ResolvedService(service_id=sid, name=name)

    # 2. Known alias equivalence — verified synonyms only, no scoring.
    for sid, name in services:
        name_l = name.lower()
        for canonical, synonyms in _SERVICE_ALIASES.items():
            if canonical in name_l and any(s in phrase for s in synonyms):
                return ResolvedService(service_id=sid, name=name)

    return None


async def resolve_resource_for_service(
    session: AsyncSession, business_id: int, service_id: int
) -> ResolvedResource | None:
    """Pick an eligible resource for a service, tenant-scoped. First eligible."""
    rows = await session.execute(
        sql_text(
            "SELECT r.id, r.name FROM resources r "
            "JOIN service_resource_eligibility e "
            "  ON e.resource_id = r.id AND e.business_id = r.business_id "
            "WHERE e.service_id = :sid AND e.business_id = :bid "
            "  AND r.is_active = true "
            "ORDER BY r.id LIMIT 1"
        ),
        {"sid": service_id, "bid": business_id},
    )
    r = rows.first()
    if r is None:
        return None
    return ResolvedResource(resource_id=r[0], name=r[1])


async def day_availability(
    session: AsyncSession, business_id: int, timezone: str, target_date: date
):
    """Structured DayAvailability from the DB — the shape the BookingCollection
    state machine needs to VALIDATE a caller's chosen time against real slots.

    Without this, the injector passes availability=None and the state machine
    can never match "10 மணி" to an offered slot → selected_time stays None →
    the booking never reaches confirmation. Returns local-time AvailableSlots
    across all service→resource pairings for the day.
    """
    from fonely.services.availability import AvailabilityService
    from .context import AvailableSlot, DayAvailability, SlotStatus

    tz = ZoneInfo(timezone)
    rows = await session.execute(
        sql_text(
            "SELECT e.service_id, e.resource_id, r.name "
            "FROM service_resource_eligibility e "
            "JOIN resources r ON r.id = e.resource_id "
            "JOIN services s ON s.id = e.service_id "
            "WHERE e.business_id = :bid AND r.is_active AND s.is_active"
        ),
        {"bid": business_id},
    )
    pairings = [(r[0], r[1], r[2]) for r in rows.fetchall()]

    svc = AvailabilityService(session)
    seen: set = set()
    slots: list = []
    for service_id, resource_id, resource_name in pairings:
        for s in await svc.get_available_slots(
            business_id=business_id, service_id=service_id,
            resource_id=resource_id, target_date=target_date,
        ):
            local_start = s.start_at.astimezone(tz).time()
            local_end = s.end_at.astimezone(tz).time()
            key = (resource_id, local_start)
            if key in seen:
                continue
            seen.add(key)
            slots.append(AvailableSlot(
                resource_id=resource_id, resource_name=resource_name,
                start_time=local_start, end_time=local_end,
                service_name="", status=SlotStatus.AVAILABLE,
            ))

    return DayAvailability(
        business_date=target_date,
        day_of_week=target_date.strftime("%A").lower(),
        is_operating_day=len(slots) > 0,
        is_exception_day=False,
        available_slots=tuple(slots),
    )


async def available_slots_text(
    session: AsyncSession, business_id: int, timezone: str, target_date: date
) -> str:
    """Render available slots for a day as system-prompt text, from the DB.

    Iterates every service→resource pairing for the tenant. Slots come from
    AvailabilityService (operating hours minus exceptions minus bookings).
    """
    from fonely.services.availability import AvailabilityService

    tz = ZoneInfo(timezone)
    rows = await session.execute(
        sql_text(
            "SELECT e.service_id, e.resource_id, r.name "
            "FROM service_resource_eligibility e "
            "JOIN resources r ON r.id = e.resource_id "
            "JOIN services s ON s.id = e.service_id "
            "WHERE e.business_id = :bid AND r.is_active AND s.is_active"
        ),
        {"bid": business_id},
    )
    pairings = [(r[0], r[1], r[2]) for r in rows.fetchall()]

    svc = AvailabilityService(session)
    resource_slots: dict[str, set[str]] = {}
    for service_id, resource_id, resource_name in pairings:
        slots = await svc.get_available_slots(
            business_id=business_id,
            service_id=service_id,
            resource_id=resource_id,
            target_date=target_date,
        )
        for s in slots:
            local = s.start_at.astimezone(tz)
            resource_slots.setdefault(resource_name, set()).add(local.strftime("%H:%M"))

    if not resource_slots:
        return (
            f"{target_date.strftime('%A %B %d')}: No confirmed availability. "
            "The doctor has not confirmed this day's schedule. Tell the caller "
            "you will check with the doctor and ask them to wait."
        )

    lines = [
        f"  {name}: {', '.join(sorted(resource_slots[name]))}"
        for name in sorted(resource_slots)
    ]
    return f"Available slots for {target_date.strftime('%A %B %d')}:\n" + "\n".join(lines)


async def clinic_context_text(session: AsyncSession, business_id: int) -> str:
    """Full clinic context for the system prompt, entirely from the DB."""
    biz = await resolve_business(session, business_id)
    tz = ZoneInfo(biz.timezone)
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)

    today_slots = await available_slots_text(session, business_id, biz.timezone, today)
    tomorrow_slots = await available_slots_text(session, business_id, biz.timezone, tomorrow)

    rows = await session.execute(
        sql_text("SELECT name FROM resources WHERE business_id = :id AND is_active"),
        {"id": business_id},
    )
    doctors = [r[0] for r in rows.fetchall()]

    rows = await session.execute(
        sql_text(
            "SELECT name, price FROM services "
            "WHERE business_id = :id AND is_active = true"
        ),
        {"id": business_id},
    )
    services = [(r[0], float(r[1]) if r[1] else 0) for r in rows.fetchall()]

    services_text = ", ".join(f"{name} ₹{int(price)}" for name, price in services)
    doctors_text = ", ".join(doctors) if doctors else "the doctor"

    return (
        f"Clinic: {biz.name}.\n"
        f"Doctors: {doctors_text}.\n"
        f"Services: {services_text}.\n"
        f"{today_slots}\n"
        f"{tomorrow_slots}"
    )


@dataclass(frozen=True)
class BookingOutcome:
    success: bool
    appointment_id: int | None = None
    # Facts below come from the commit RECEIPT (the engine), never the LLM.
    service_name: str = ""
    resource_name: str = ""
    error: str = ""


async def book_appointment(
    *,
    command_port,
    session: AsyncSession,
    business_id: int,
    service_phrase: str,
    target_date: date,
    target_time: time,
    idempotency_key: str,
) -> BookingOutcome:
    """The SINGLE function that commits a voice booking.

    It resolves identity (service/resource) from the DB, then commits ONLY
    through the injected CommandPort. It never constructs AppointmentService
    itself — that is the port's job, and the port is the one place a commit
    is built. test_single_commit_path enforces this structurally.

    Refuses (does not commit) when the service phrase matches no real service
    for this tenant, when no resource is eligible, or when the port refuses
    (e.g. the requested time is outside operating hours — the refusal that
    matters). Confirmation facts are read from the receipt, never inferred.
    """
    from .runtime import ProposeCommand, ConfirmCommand, TrustedCommandContext

    svc = await resolve_service(session, business_id, service_phrase)
    if svc is None:
        return BookingOutcome(success=False, error=f"unknown_service:{service_phrase}")

    res = await resolve_resource_for_service(session, business_id, svc.service_id)
    if res is None:
        return BookingOutcome(success=False, error=f"no_resource:{svc.name}")

    ctx = TrustedCommandContext(
        business_id=business_id,
        actor_session_id=idempotency_key,
        conversation_id=idempotency_key,
    )

    propose_result = await command_port.propose(
        ProposeCommand(
            context=ctx,
            service_id=svc.service_id,
            resource_id=res.resource_id,
            target_date=target_date,
            target_time=target_time.strftime("%H:%M"),
            idempotency_key=idempotency_key,
        )
    )
    if not propose_result.success:
        return BookingOutcome(success=False, error=propose_result.error or "propose_failed")

    confirm_result = await command_port.confirm(
        ConfirmCommand(
            context=ctx,
            proposal_id=propose_result.proposal_id,
            idempotency_key=idempotency_key,
            expected_version=int(propose_result.evidence.get("version", 0)),
        )
    )
    if not confirm_result.success or confirm_result.receipt is None:
        return BookingOutcome(success=False, error=confirm_result.error or "confirm_failed")

    # Facts from the RECEIPT, not the LLM, not the resolver.
    facts = confirm_result.receipt.facts
    return BookingOutcome(
        success=True,
        appointment_id=confirm_result.receipt.commitment_id,
        service_name=facts.get("service_name", ""),
        resource_name=facts.get("resource_name", ""),
    )
