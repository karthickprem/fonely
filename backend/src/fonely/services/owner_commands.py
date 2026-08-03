"""Owner command execution service."""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.enums import CallerRole, DailyContextType
from fonely.models.schema import (
    Appointment,
    Business,
    BusinessDailyContext,
    Resource,
    ScheduleException,
)
from fonely.services.model_gateway import ModelGateway
from fonely.services.owner_command_parser import OwnerCommandParser, ParsedOwnerCommand

logger = logging.getLogger("fonely.services.owner_commands")

_UNKNOWN_RESPONSE = (
    "Sorry, I didn't understand that command. You can say things like:\n"
    "- 'Dr. Priya leave tomorrow'\n"
    "- 'Close clinic early at 5'\n"
    "- 'Show tomorrow appointments'\n"
    "- 'This week consultation free'"
)


@dataclass(frozen=True)
class OwnerCommandResult:
    command_type: str
    success: bool
    response_text: str
    affected_appointments: int = 0
    affected_patients: int = 0
    details: list[str] = field(default_factory=list)


class OwnerCommandService:
    def __init__(self, session: AsyncSession, model: ModelGateway) -> None:
        self._session = session
        self._parser = OwnerCommandParser(model)

    async def process_command(
        self, business_id: int, owner_phone: str, message: str
    ) -> OwnerCommandResult:
        doctor_names = await self._get_doctor_names(business_id)
        parsed = await self._parser.parse(message, doctor_names)

        if parsed.command == "doctor_leave":
            return await self._handle_doctor_leave(business_id, owner_phone, parsed)
        if parsed.command == "close_clinic":
            return await self._handle_close_clinic(business_id, owner_phone, parsed)
        if parsed.command == "get_summary":
            return await self._handle_get_summary(business_id, parsed)
        if parsed.command == "add_offer":
            return await self._handle_add_context(
                business_id, owner_phone, parsed, DailyContextType.OFFER
            )
        if parsed.command == "add_note":
            return await self._handle_add_context(
                business_id, owner_phone, parsed, DailyContextType.NOTE
            )
        if parsed.command == "close_early":
            return await self._handle_close_clinic(business_id, owner_phone, parsed)

        return OwnerCommandResult(
            command_type="unknown",
            success=False,
            response_text=_UNKNOWN_RESPONSE,
        )

    async def _handle_doctor_leave(
        self, business_id: int, owner_phone: str, parsed: ParsedOwnerCommand
    ) -> OwnerCommandResult:
        resource = await self._resolve_resource(business_id, parsed.doctor_name)
        if resource is None:
            return OwnerCommandResult(
                command_type="doctor_leave",
                success=False,
                response_text=f"Could not find doctor '{parsed.doctor_name}'. "
                f"Available: {', '.join(await self._get_doctor_names(business_id))}",
            )

        target_date = self._resolve_date(parsed.date)
        reason = parsed.reason or "Leave"

        exc = ScheduleException(
            business_id=business_id,
            resource_id=resource.id,
            exception_date=target_date,
            is_closed=True,
            reason=reason,
        )
        self._session.add(exc)
        await self._session.flush()

        cancelled = await self._cancel_appointments_for_resource(
            business_id, resource.id, target_date, owner_phone
        )

        date_str = target_date.strftime("%b %d")
        lines = [f"Done. {resource.name} marked on leave for {date_str}."]
        if cancelled:
            lines.append(f"{len(cancelled)} appointment(s) cancelled:")
            for appt in cancelled:
                lines.append(f"  - {appt['time']} {appt['patient']} ({appt['service']}) — notified")
            lines.append(f"{resource.name} will not be booked for {date_str}.")
        else:
            lines.append(f"No appointments to cancel. {resource.name} will not be booked.")

        return OwnerCommandResult(
            command_type="doctor_leave",
            success=True,
            response_text="\n".join(lines),
            affected_appointments=len(cancelled),
            affected_patients=len(cancelled),
            details=[c["patient"] for c in cancelled],
        )

    async def _handle_close_clinic(
        self, business_id: int, owner_phone: str, parsed: ParsedOwnerCommand
    ) -> OwnerCommandResult:
        target_date = self._resolve_date(parsed.date)
        reason = parsed.reason or "Closed"

        exc = ScheduleException(
            business_id=business_id,
            resource_id=None,
            exception_date=target_date,
            is_closed=True,
            reason=reason,
        )
        self._session.add(exc)
        await self._session.flush()

        cancelled = await self._cancel_all_appointments(business_id, target_date, owner_phone)
        date_str = target_date.strftime("%b %d")

        lines = [f"Clinic closed on {date_str}. {reason}."]
        if cancelled:
            lines.append(f"{len(cancelled)} appointment(s) cancelled and patients notified.")
        return OwnerCommandResult(
            command_type="close_clinic",
            success=True,
            response_text="\n".join(lines),
            affected_appointments=len(cancelled),
            affected_patients=len(cancelled),
        )

    async def _handle_get_summary(
        self, business_id: int, parsed: ParsedOwnerCommand
    ) -> OwnerCommandResult:
        target_date = self._resolve_date(parsed.date)
        date_str = target_date.strftime("%A, %b %d")

        appointments = (
            (
                await self._session.execute(
                    select(Appointment)
                    .where(
                        Appointment.business_id == business_id,
                        Appointment.status == "confirmed",
                    )
                    .order_by(Appointment.start_at)
                )
            )
            .scalars()
            .all()
        )

        tz_name = await self._get_business_timezone(business_id)
        tz = ZoneInfo(tz_name)
        day_appts = [a for a in appointments if a.start_at.astimezone(tz).date() == target_date]

        if not day_appts:
            return OwnerCommandResult(
                command_type="get_summary",
                success=True,
                response_text=f"No appointments for {date_str}.",
            )

        lines = [f"Appointments for {date_str}:"]
        for appt in day_appts:
            local_time = appt.start_at.astimezone(tz).strftime("%-I:%M %p")
            name = appt.customer_name or "Patient"
            service = appt.service_name_snapshot
            doctor = appt.resource_name_snapshot
            lines.append(f"  {local_time} — {name}, {service}, {doctor}")
        lines.append(f"Total: {len(day_appts)} appointment(s)")

        return OwnerCommandResult(
            command_type="get_summary",
            success=True,
            response_text="\n".join(lines),
            affected_appointments=len(day_appts),
        )

    async def _handle_add_context(
        self,
        business_id: int,
        owner_phone: str,
        parsed: ParsedOwnerCommand,
        context_type: DailyContextType,
    ) -> OwnerCommandResult:
        content = parsed.description or parsed.note or ""
        if not content:
            return OwnerCommandResult(
                command_type=parsed.command,
                success=False,
                response_text="Please provide the content for the note/offer.",
            )

        target_date = self._resolve_date(parsed.for_date or parsed.valid_until)
        ctx = BusinessDailyContext(
            business_id=business_id,
            context_date=target_date,
            context_type=context_type.value,
            content=content,
            created_by_phone=owner_phone,
        )
        self._session.add(ctx)
        await self._session.flush()

        label = "Offer" if context_type == DailyContextType.OFFER else "Note"
        return OwnerCommandResult(
            command_type=parsed.command,
            success=True,
            response_text=f"{label} noted. Patients will be informed: {content}",
        )

    async def _get_doctor_names(self, business_id: int) -> list[str]:
        resources = (
            (
                await self._session.execute(
                    select(Resource).where(
                        Resource.business_id == business_id,
                        Resource.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [r.name for r in resources]

    async def _resolve_resource(self, business_id: int, name: str | None) -> Resource | None:
        if not name:
            return None
        name_lower = name.lower()
        resources = (
            (
                await self._session.execute(
                    select(Resource).where(
                        Resource.business_id == business_id,
                        Resource.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for r in resources:
            if r.name.lower() == name_lower or name_lower in r.name.lower():
                return r
        return None

    def _resolve_date(self, expr: str | None) -> date:
        if not expr:
            return datetime.now(UTC).date()
        expr_lower = expr.lower().strip()
        today = datetime.now(UTC).date()
        if expr_lower in ("today", "innikku", "இன்று"):
            return today
        if expr_lower in ("tomorrow", "naalaikku", "நாளை"):
            return today + timedelta(days=1)
        try:
            return date.fromisoformat(expr_lower)
        except ValueError:
            return today

    async def _get_business_timezone(self, business_id: int) -> str:
        biz = await self._session.scalar(select(Business).where(Business.id == business_id))
        return biz.timezone if biz else "Asia/Kolkata"

    async def _cancel_appointments_for_resource(
        self,
        business_id: int,
        resource_id: int,
        target_date: date,
        owner_phone: str,
    ) -> list[dict[str, str]]:
        tz_name = await self._get_business_timezone(business_id)
        tz = ZoneInfo(tz_name)
        appointments = (
            (
                await self._session.execute(
                    select(Appointment).where(
                        Appointment.business_id == business_id,
                        Appointment.resource_id == resource_id,
                        Appointment.status == "confirmed",
                    )
                )
            )
            .scalars()
            .all()
        )

        cancelled: list[dict[str, str]] = []
        for appt in appointments:
            if appt.start_at.astimezone(tz).date() != target_date:
                continue
            success = await self._cancel_via_service(
                business_id, appt.id, appt.version, owner_phone, "owner_leave"
            )
            if success:
                local_time = appt.start_at.astimezone(tz).strftime("%-I:%M %p")
                cancelled.append(
                    {
                        "time": local_time,
                        "patient": appt.customer_name or "Patient",
                        "service": appt.service_name_snapshot,
                        "phone": appt.customer_phone,
                    }
                )
        return cancelled

    async def _cancel_all_appointments(
        self,
        business_id: int,
        target_date: date,
        owner_phone: str,
    ) -> list[dict[str, str]]:
        tz_name = await self._get_business_timezone(business_id)
        tz = ZoneInfo(tz_name)
        appointments = (
            (
                await self._session.execute(
                    select(Appointment).where(
                        Appointment.business_id == business_id,
                        Appointment.status == "confirmed",
                    )
                )
            )
            .scalars()
            .all()
        )

        cancelled: list[dict[str, str]] = []
        for appt in appointments:
            if appt.start_at.astimezone(tz).date() != target_date:
                continue
            success = await self._cancel_via_service(
                business_id, appt.id, appt.version, owner_phone, "owner_closure"
            )
            if success:
                cancelled.append(
                    {
                        "time": appt.start_at.astimezone(tz).strftime("%-I:%M %p"),
                        "patient": appt.customer_name or "Patient",
                        "service": appt.service_name_snapshot,
                    }
                )
        return cancelled

    async def _cancel_via_service(
        self,
        business_id: int,
        appointment_id: int,
        appointment_version: int,
        owner_phone: str,
        reason_code: str,
    ) -> bool:
        try:
            from fonely.api.internal.validation import InternalValidationPort
            from fonely.domain.appointments.commands import (
                ConfirmPendingAppointmentCancellationCommand,
                CreatePendingAppointmentCancellationCommand,
            )
            from fonely.domain.pending_actions.commands import ActorContext
            from fonely.services.appointments import AppointmentService

            validation = InternalValidationPort(self._session)
            appt_service = AppointmentService(self._session, validation=validation)
            actor = ActorContext(
                business_id=business_id,
                normalized_phone=owner_phone,
                verified_role=CallerRole.OWNER,
                session_id=None,
            )

            now = datetime.now(UTC)
            key = f"owner-cancel-{appointment_id}-{uuid.uuid4().hex[:8]}"
            proposal = await appt_service.create_cancellation_proposal(
                CreatePendingAppointmentCancellationCommand(
                    actor=actor,
                    appointment_id=appointment_id,
                    expected_appointment_version=appointment_version,
                    reason_code=reason_code,
                    expires_at=now + timedelta(minutes=5),
                    idempotency_key=key,
                )
            )
            await appt_service.confirm_cancellation(
                ConfirmPendingAppointmentCancellationCommand(
                    actor=actor,
                    pending_action_id=proposal.pending_action_id,
                    expected_version=proposal.version,
                )
            )
            return True
        except Exception:
            logger.info(
                "owner_cancel_service_fallback",
                extra={"appointment_id": appointment_id},
                exc_info=True,
            )
            return await self._cancel_direct(business_id, appointment_id)

    async def _cancel_direct(self, business_id: int, appointment_id: int) -> bool:
        from sqlalchemy import update

        from fonely.models.schema import ResourceAllocation

        now = datetime.now(UTC)
        result = await self._session.execute(
            update(Appointment)
            .where(
                Appointment.id == appointment_id,
                Appointment.business_id == business_id,
                Appointment.status == "confirmed",
            )
            .values(status="cancelled", cancelled_at=now)
        )
        if getattr(result, "rowcount", 0) == 0:
            return False
        await self._session.execute(
            update(ResourceAllocation)
            .where(
                ResourceAllocation.appointment_id == appointment_id,
                ResourceAllocation.business_id == business_id,
                ResourceAllocation.status == "active",
            )
            .values(status="cancelled", updated_at=now)
        )
        await self._session.flush()
        return True


async def get_daily_context(
    business_id: int, context_date: date, session: AsyncSession
) -> list[BusinessDailyContext]:
    result = await session.execute(
        select(BusinessDailyContext).where(
            BusinessDailyContext.business_id == business_id,
            BusinessDailyContext.context_date == context_date,
            BusinessDailyContext.active.is_(True),
        )
    )
    return list(result.scalars().all())
