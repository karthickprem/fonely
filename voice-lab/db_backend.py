"""Production backend adapter — one source of truth: PostgreSQL.

Owner commands → OwnerCommandService → schedule_exceptions in DB
Availability → AvailabilityService → reads operating_schedules + exceptions
Booking → AppointmentService → writes appointments

No in-memory state. No hardcoded slots.
"""
import os
import sys
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Add main backend to path
_MAIN_SRC = "/scratch/karthick/fonely/backend/src"
if _MAIN_SRC not in sys.path:
    sys.path.insert(0, _MAIN_SRC)

# The demo owner panel writes schedule_exceptions to the SAME DB the agent
# reads: fonely_dev4. Bind explicitly (see production_wiring for why the
# ambient env couldn't be trusted). Still load the rest of .env for provider
# keys. This is demo wiring, not shippable config.
_db_url = "postgresql+asyncpg://localhost:5432/fonely_dev4"
with open("/scratch/karthick/fonely/.env") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#") and not line.startswith("DATABASE_URL="):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

if _db_url and "asyncpg" not in _db_url:
    if "://" in _db_url:
        _, _rest = _db_url.split("://", 1)
        _db_url = f"postgresql+asyncpg://{_rest}"
_engine = create_async_engine(_db_url, pool_size=5)
SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)

logger = logging.getLogger("fonely.voice.db_backend")

# The demo binds to ONE business via its dialed number. In production this
# comes from the Exotel inbound number → business_id mapping. Here it's the
# only active business in the DB. NO hardcoded service/resource IDs — those
# are resolved from the DB per request so the demo works against any clinic.
DEMO_BUSINESS_ID = int(os.environ.get("DEMO_BUSINESS_ID", "1"))
OWNER_PHONE = "+919000000000"
CUSTOMER_PHONE = "+919000000001"


async def resolve_business() -> dict:
    """Resolve the demo business + timezone from the DB. No hardcoding."""
    from sqlalchemy import text as sql_text
    async with SessionLocal() as session:
        row = await session.execute(
            sql_text("SELECT id, name, timezone FROM businesses WHERE id = :id"),
            {"id": DEMO_BUSINESS_ID},
        )
        r = row.first()
        if r is None:
            raise RuntimeError(f"Business {DEMO_BUSINESS_ID} not found")
        return {"business_id": r[0], "name": r[1], "timezone": r[2]}


async def resolve_service(service_phrase: str) -> dict | None:
    """Map a caller/LLM service phrase to a real DB service. Returns None if
    no confident match — caller must never book an invented service."""
    from sqlalchemy import text as sql_text
    phrase = service_phrase.lower().strip()
    async with SessionLocal() as session:
        rows = await session.execute(
            sql_text(
                "SELECT id, name, price, duration_minutes FROM services "
                "WHERE business_id = :bid AND is_active = true"
            ),
            {"bid": DEMO_BUSINESS_ID},
        )
        services = [
            {"id": r[0], "name": r[1], "price": float(r[2]) if r[2] else 0, "duration": r[3]}
            for r in rows.fetchall()
        ]
    # Match by name substring in either direction
    for svc in services:
        name_l = svc["name"].lower()
        if name_l in phrase or phrase in name_l:
            return svc
    # Common aliases → canonical service names
    aliases = {
        "scaling": ["cleaning", "polish", "scal"],
        "cleaning": ["scaling", "clean", "polish"],
        "checkup": ["consultation", "check", "review"],
        "consultation": ["checkup", "consult"],
        "extraction": ["remove", "pull", "extract"],
        "filling": ["cavity", "fill"],
        "root canal": ["rct", "canal"],
    }
    for svc in services:
        name_l = svc["name"].lower()
        for canonical, alist in aliases.items():
            if canonical in name_l and any(a in phrase for a in alist):
                return svc
    return None


async def resolve_resource_for_service(service_id: int) -> dict | None:
    """Pick an eligible resource (doctor) for a service. First eligible."""
    from sqlalchemy import text as sql_text
    async with SessionLocal() as session:
        rows = await session.execute(
            sql_text(
                "SELECT r.id, r.name FROM resources r "
                "JOIN service_resource_eligibility e ON e.resource_id = r.id "
                "WHERE e.service_id = :sid AND e.business_id = :bid AND r.is_active = true "
                "ORDER BY r.id LIMIT 1"
            ),
            {"sid": service_id, "bid": DEMO_BUSINESS_ID},
        )
        r = rows.first()
        if r is None:
            return None
        return {"id": r[0], "name": r[1]}


async def get_available_slots_text(target_date: date | None = None) -> str:
    """Get real available slots from DB for a day, across all services/resources.

    Fully DB-driven: iterates every active service and its eligible resources.
    No hardcoded service/resource IDs.
    """
    from fonely.services.availability import AvailabilityService
    from sqlalchemy import text as sql_text

    biz = await resolve_business()
    tz = ZoneInfo(biz["timezone"])
    if target_date is None:
        target_date = datetime.now(tz).date()

    # Gather all service→resource pairings from DB
    async with SessionLocal() as session:
        rows = await session.execute(
            sql_text(
                "SELECT e.service_id, e.resource_id, r.name "
                "FROM service_resource_eligibility e "
                "JOIN resources r ON r.id = e.resource_id "
                "JOIN services s ON s.id = e.service_id "
                "WHERE e.business_id = :bid AND r.is_active AND s.is_active"
            ),
            {"bid": biz["business_id"]},
        )
        pairings = [(r[0], r[1], r[2]) for r in rows.fetchall()]

        svc = AvailabilityService(session)
        # Collect unique available slot times per resource
        resource_slots: dict[str, set] = {}
        for service_id, resource_id, resource_name in pairings:
            slots = await svc.get_available_slots(
                business_id=biz["business_id"],
                service_id=service_id,
                resource_id=resource_id,
                target_date=target_date,
            )
            for s in slots:
                local = s.start_at.astimezone(tz)
                resource_slots.setdefault(resource_name, set()).add(local.strftime("%H:%M"))

    if not resource_slots:
        return (f"{target_date.strftime('%A %B %d')}: No confirmed availability. "
                "Doctor has not confirmed the schedule. Tell the patient you will "
                "check with the doctor and ask them to wait.")

    lines = []
    for resource_name in sorted(resource_slots):
        times = sorted(resource_slots[resource_name])
        lines.append(f"  {resource_name}: {', '.join(times)}")

    return f"Available slots for {target_date.strftime('%A %B %d')}:\n" + "\n".join(lines)


async def get_clinic_context() -> str:
    """Full clinic context from DB for system prompt. Fully DB-driven."""
    from sqlalchemy import text as sql_text

    biz = await resolve_business()
    tz = ZoneInfo(biz["timezone"])
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)

    today_slots = await get_available_slots_text(today)
    tomorrow_slots = await get_available_slots_text(tomorrow)

    async with SessionLocal() as session:
        rows = await session.execute(
            sql_text("SELECT name FROM resources WHERE business_id = :id AND is_active"),
            {"id": biz["business_id"]},
        )
        doctors = [r[0] for r in rows.fetchall()]

        rows = await session.execute(
            sql_text("SELECT name, price FROM services WHERE business_id = :id AND is_active = true"),
            {"id": biz["business_id"]},
        )
        services = [(r[0], float(r[1]) if r[1] else 0) for r in rows.fetchall()]

    services_text = ", ".join(f"{name} ₹{int(price)}" for name, price in services)
    doctors_text = ", ".join(doctors) if doctors else "the doctor"

    return (
        f"Clinic: {biz['name']}.\n"
        f"Doctors: {doctors_text}.\n"
        f"Services: {services_text}.\n"
        f"{today_slots}\n"
        f"{tomorrow_slots}"
    )


def _parse_owner_time(lower: str) -> time | None:
    """Extract a clock time from an owner command. Handles '12', '12 noon',
    '3 pm', '12:30', 'noon'. Returns None if no time present."""
    import re as _re
    if "noon" in lower and not _re.search(r"\d", lower):
        return time(12, 0)
    m = _re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|noon)?", lower)
    if not m:
        return None
    h = int(m.group(1))
    minute = int(m.group(2) or 0)
    mer = m.group(3)
    if mer == "pm" and h < 12:
        h += 12
    elif mer == "noon":
        h = 12
    elif mer is None:
        # bare hour: clinic runs 09:30-13:00 and 17:00-20:30. 1-8 → afternoon.
        if 1 <= h <= 8:
            h += 12
    if h > 23:
        return None
    return time(h, minute)


async def _resolve_named_resource(session, business_id: int, lower: str):
    """Find a resource whose name a doctor-specific command mentions.
    Returns (id, name) or None (→ command applies to all doctors)."""
    from sqlalchemy import text as sql_text
    rows = await session.execute(
        sql_text("SELECT id, name FROM resources WHERE business_id = :bid AND is_active"),
        {"bid": business_id},
    )
    for rid, name in [(r[0], r[1]) for r in rows.fetchall()]:
        # Match on any name token ("priya" in "Dr. Priya Venkatesan").
        for tok in name.lower().replace("dr.", "").replace("dr ", "").split():
            if len(tok) >= 3 and tok in lower:
                return rid, name
    return None


async def process_owner_command(text: str) -> dict:
    """Process owner command → schedule_exceptions in PostgreSQL.

    Handles, per-doctor or clinic-wide, for today or tomorrow:
      - full closure: "Dr Priya leave tomorrow", "clinic closed today"
      - not-available-at-a-time: "Dr Priya not available at 12 noon tomorrow"
        → writes a modified-hours exception that removes that slot. With the
        single-window schema, "not free at T" restricts the doctor's shift to
        end before T (if T is in the morning) or start after T (if evening).
      - reopen: "Dr Priya available again today" / "today open"
    """
    from sqlalchemy import text as sql_text
    from fonely.models.schema import ScheduleException

    biz = await resolve_business()
    lower = text.lower()
    tz = ZoneInfo(biz["timezone"])
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    target_date = tomorrow if any(w in lower for w in ("tomorrow", "நாளை", "naalai")) else today
    day_label = "tomorrow" if target_date == tomorrow else "today"

    is_negation = any(w in lower for w in ("not available", "not free", "won't", "wont", "no ", "leave", "off", "closed", "close", "busy", "cannot", "can't", "இல்ல", "வேண்டாம்"))
    at_time = _parse_owner_time(lower)

    async with SessionLocal() as session:
        named = await _resolve_named_resource(session, biz["business_id"], lower)
        rows = await session.execute(
            sql_text("SELECT id, name FROM resources WHERE business_id = :bid AND is_active"),
            {"bid": biz["business_id"]},
        )
        all_resources = [(r[0], r[1]) for r in rows.fetchall()]
        targets = [named] if named else all_resources
        who = named[1] if named else "All doctors"

        # 1) NOT-AVAILABLE-AT-A-TIME (specific-time block).
        if is_negation and at_time is not None:
            for rid, rname in targets:
                # Fetch this resource's base shifts for the weekday to know how
                # to carve. Clinic default: 09:30-13:00 morning, 17:00-20:30 eve.
                dow = target_date.weekday()
                srows = await session.execute(
                    sql_text("SELECT open_time, close_time FROM operating_schedules "
                             "WHERE business_id=:bid AND (resource_id=:rid OR resource_id IS NULL) "
                             "AND day_of_week=:d AND is_active ORDER BY open_time"),
                    {"bid": biz["business_id"], "rid": rid, "d": dow},
                )
                shifts = [(r[0], r[1]) for r in srows.fetchall()]
                # Find the shift containing at_time; shrink it to exclude at_time.
                new_open, new_close = None, None
                for op, cl in shifts:
                    if op <= at_time < cl:
                        # Morning slot blocked → end shift before at_time.
                        # Evening slot blocked → start shift after at_time.
                        if at_time.hour < 14:
                            new_open, new_close = op, at_time
                        else:
                            # start just after the blocked slot (assume 15-min grid)
                            after = time((at_time.hour + (at_time.minute + 15)//60) % 24, (at_time.minute + 15) % 60)
                            new_open, new_close = after, cl
                        break
                # Clear any prior exception for this resource/date, then write.
                await session.execute(
                    sql_text("DELETE FROM schedule_exceptions WHERE business_id=:bid AND resource_id=:rid AND exception_date=:d"),
                    {"bid": biz["business_id"], "rid": rid, "d": target_date},
                )
                if new_open and new_close and new_close > new_open:
                    session.add(ScheduleException(
                        business_id=biz["business_id"], resource_id=rid,
                        exception_date=target_date, is_closed=False,
                        open_time=new_open, close_time=new_close, reason=text,
                    ))
            await session.commit()
            context = await get_clinic_context()
            return {
                "success": True, "command_type": "block_time",
                "message": f"{who} {day_label} ({target_date}): {at_time.strftime('%H:%M')} slot removed. Hours adjusted in database.",
                "context": context,
            }

        # 2) FULL CLOSURE (negation, no specific time).
        if is_negation:
            for rid, rname in targets:
                await session.execute(
                    sql_text("DELETE FROM schedule_exceptions WHERE business_id=:bid AND resource_id=:rid AND exception_date=:d"),
                    {"bid": biz["business_id"], "rid": rid, "d": target_date},
                )
                session.add(ScheduleException(
                    business_id=biz["business_id"], resource_id=rid,
                    exception_date=target_date, is_closed=True, reason=text,
                ))
            await session.commit()
            context = await get_clinic_context()
            return {
                "success": True, "command_type": "close",
                "message": f"{who} {day_label} ({target_date}) marked closed in database.",
                "context": context,
            }

        # 3) REOPEN — remove exceptions (only when NOT a negation).
        if any(w in lower for w in ("open", "reopen", "available", "back", "free")):
            for rid, rname in targets:
                await session.execute(
                    sql_text("DELETE FROM schedule_exceptions WHERE business_id=:bid AND resource_id=:rid AND exception_date=:d"),
                    {"bid": biz["business_id"], "rid": rid, "d": target_date},
                )
            await session.commit()
            context = await get_clinic_context()
            return {
                "success": True, "command_type": "reopen",
                "message": f"{who} {day_label} ({target_date}) reopened — exceptions removed.",
                "context": context,
            }

    context = await get_clinic_context()
    return {
        "success": False, "command_type": "unknown",
        "message": f"Didn't understand: '{text}'. Try: 'Dr Priya not available at 12 tomorrow', 'today leave', 'today open'.",
        "context": context,
    }


async def book_appointment(
    service_name: str,
    target_date: date,
    target_time: time,
    patient_name: str,
    session_id: str = "voice-demo",
) -> dict:
    """Book through real AppointmentService → PostgreSQL.

    TRUSTED-FACTS INVARIANT: service_id and resource_id are resolved from the
    DB by name here — they are NEVER supplied by the LLM. The committed
    appointment carries DB facts (service_name, price, duration), not model
    output. If the caller's service phrase doesn't match a real service,
    booking is refused rather than inventing one.
    """
    from fonely.services.appointments import AppointmentService
    from fonely.domain.appointments.commands import (
        CreatePendingAppointmentCommand,
        ConfirmPendingAppointmentCommand,
    )
    from fonely.domain.appointments.results import PreCommitAppointmentSuccess
    from fonely.domain.pending_actions.commands import ActorContext
    from fonely.models.enums import CallerRole
    from fonely.api.internal.validation import InternalValidationPort

    biz = await resolve_business()
    tz = ZoneInfo(biz["timezone"])

    # Resolve service from DB — refuse if no match (never invent a service)
    svc_match = await resolve_service(service_name)
    if svc_match is None:
        return {"success": False, "error": f"unknown_service:{service_name}"}

    # Resolve eligible resource from DB
    res_match = await resolve_resource_for_service(svc_match["id"])
    if res_match is None:
        return {"success": False, "error": f"no_resource_for_service:{svc_match['name']}"}

    start_at = datetime(
        target_date.year, target_date.month, target_date.day,
        target_time.hour, target_time.minute, tzinfo=tz,
    )

    actor = ActorContext(
        business_id=biz["business_id"],
        normalized_phone=CUSTOMER_PHONE,
        verified_role=CallerRole.CUSTOMER,
        session_id=f"voice-{session_id}",
    )

    async with SessionLocal() as session:
        validation = InternalValidationPort(session)
        service = AppointmentService(session, validation=validation)

        expires_at = datetime.now(tz=ZoneInfo("UTC")) + timedelta(minutes=15)
        proposal = await service.create_proposal(
            CreatePendingAppointmentCommand(
                actor=actor,
                service_id=svc_match["id"],
                resource_id=res_match["id"],
                start_at=start_at,
                customer_name=patient_name,
                customer_phone=CUSTOMER_PHONE,
                reason=service_name,
                expires_at=expires_at,
                idempotency_key=f"voice-{session_id}-{target_date}-{target_time}",
            )
        )
        await session.commit()

        outcome = await service.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=actor,
                pending_action_id=proposal.pending_action_id,
                expected_version=proposal.version,
            )
        )
        await session.commit()

        if isinstance(outcome, PreCommitAppointmentSuccess):
            appt = outcome.appointment
            local_start = appt.start_at.astimezone(tz)
            return {
                "success": True,
                "appointment_id": appt.appointment_id,
                "service": appt.service_name,
                "resource": appt.resource_name,
                "price": float(appt.price) if appt.price else None,
                "time": local_start.strftime("%I:%M %p"),
                "date": local_start.strftime("%B %d"),
            }
        else:
            return {"success": False, "error": outcome.error_code.value}
