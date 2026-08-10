"""Durable owner command service — simplified two-phase confirmation.

Flow:
  1. preview_command  -> parse, validate, create proposal (pending_confirmation)
  2. confirm_command  -> load FOR UPDATE, check expiry, execute in savepoint,
                         CAS pending -> completed (atomically inside savepoint)
  3. reject_command   -> CAS pending -> rejected

No executing/failed intermediate states. If the savepoint fails, the proposal
stays pending and the caller can retry.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.validators import utcnow
from fonely.models.enums import (
    AppointmentStatus,
    ResourceAllocationStatus,
)
from fonely.models.schema import (
    Appointment,
    Business,
    BusinessDailyContext,
    BusinessUser,
    Resource,
    ResourceAllocation,
    ScheduleException,
)
from fonely.repositories.owner_command_proposals import OwnerCommandProposalRepository

logger = logging.getLogger("fonely.services.owner_commands")

_UNKNOWN_RESPONSE = (
    "Sorry, I didn't understand that command. You can say things like:\n"
    "- 'Close tomorrow'\n"
    "- 'Cancel all appointments tomorrow'\n"
    "- 'Show tomorrow appointments'\n"
    "- 'Dr. Priya leave tomorrow'"
)


# ---------------------------------------------------------------------------
# Result dataclass (kept for backward compatibility with existing callers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerCommandResult:
    command_type: str
    success: bool
    response_text: str
    proposal_id: str | None = None
    affected_appointments: int = 0
    affected_patients: int = 0
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Standalone daily context query
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Command patterns for regex-based parsing
# ---------------------------------------------------------------------------

_DATE_PATTERNS: dict[str, str] = {
    "today": "today",
    "innikku": "today",
    "tomorrow": "tomorrow",
    "naalaikku": "tomorrow",
}

_CLOSE_RE = re.compile(
    r"(?:close|shutdown|bandh)\s+(?:the\s+)?(?:clinic\s+)?(?:on\s+)?(.+)",
    re.IGNORECASE,
)
_CANCEL_ALL_RE = re.compile(
    r"(?:cancel)\s+(?:all\s+)?(?:appointments?\s+)?(?:on\s+|for\s+)?(.+)",
    re.IGNORECASE,
)
_DOCTOR_LEAVE_RE = re.compile(
    r"(?:dr\.?\s*|doctor\s+)(\w+)\s+(?:leave|off|absent)\s*(?:on\s+|for\s+)?(.+)?",
    re.IGNORECASE,
)
_GET_SUMMARY_RE = re.compile(
    r"(?:show|list|get|view)\s+(?:(?:tomorrow|today)\s+)?(?:appointments?|summary|schedule)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class OwnerCommandService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = OwnerCommandProposalRepository(session)

    # ===================================================================
    # Public API
    # ===================================================================

    async def preview_command(
        self, business_id: int, owner_phone: str, raw_text: str
    ) -> dict[str, Any]:
        """Parse a command, validate, and create a pending proposal."""
        # 1. Resolve owner
        owner = await self._resolve_owner(business_id, owner_phone)
        if owner is None:
            return {"error": "owner_not_found"}

        # 2. Expire any stale pending proposal
        await self._expire_stale(business_id, owner.id)

        # 3. Parse command
        parsed = self._parse_command(raw_text, business_id)
        if parsed is None:
            return {"error": "unrecognized_command", "message": _UNKNOWN_RESPONSE}

        # 4. Build snapshot of affected entities
        affected = await self._query_affected(business_id, parsed)

        # 5. Build idempotency key
        idem_key = f"owner-cmd-{parsed['command_type']}-{parsed.get('target_date', 'none')}"

        # 6. Check for completed replay
        completed = await self._repo.find_completed_by_key_prefix(business_id, idem_key)
        if completed is not None:
            return {
                "status": "already_completed",
                "proposal_id": completed.id,
                "result_summary": completed.result_summary,
            }

        # 7. Create proposal
        proposal_id = str(uuid.uuid4())
        proposal = await self._repo.create_idempotent(
            {
                "id": proposal_id,
                "business_id": business_id,
                "owner_user_id": owner.id,
                "command_type": parsed["command_type"],
                "command_payload": {**parsed, "affected_summary": affected},
                "status": "pending_confirmation",
                "idempotency_key": idem_key,
                "expires_at": utcnow() + timedelta(minutes=10),
            }
        )

        if proposal is None:
            # Partial unique conflict — pending already exists for this owner
            existing = await self._repo.get_latest_for_owner(business_id, owner.id)
            if existing is not None:
                return {
                    "status": "pending_confirmation",
                    "proposal_id": existing.id,
                    "command_type": existing.command_type,
                    "command_payload": existing.command_payload,
                    "expires_at": existing.expires_at.isoformat(),
                }
            return {"error": "proposal_conflict"}

        return {
            "status": "pending_confirmation",
            "proposal_id": proposal.id,
            "command_type": proposal.command_type,
            "command_payload": proposal.command_payload,
            "expires_at": proposal.expires_at.isoformat(),
        }

    async def confirm_command(self, business_id: int, proposal_id: str) -> dict[str, Any]:
        """Load pending proposal FOR UPDATE, execute in savepoint, CAS to completed."""
        proposal = await self._repo.get_by_id(business_id, proposal_id)
        if proposal is None:
            return {"error": "proposal_not_found"}

        if proposal.status != "pending_confirmation":
            return {"error": "invalid_status", "current_status": proposal.status}

        now = utcnow()
        if proposal.expires_at <= now:
            await self._repo.transition_status(
                proposal.id, business_id, proposal.expected_version, "expired"
            )
            return {"error": "proposal_expired"}

        # Execute command inside savepoint — both the domain mutation and
        # the CAS transition live in the same savepoint so they commit or
        # roll back atomically.
        result_summary: dict[str, Any] = {}
        try:
            async with self._session.begin_nested():
                result_summary = await self._execute_command(
                    business_id, proposal.command_type, proposal.command_payload
                )
                # CAS pending -> completed LAST inside savepoint
                updated = await self._repo.transition_status(
                    proposal.id,
                    business_id,
                    proposal.expected_version,
                    "completed",
                    result_summary=result_summary,
                )
                if updated is None:
                    raise RuntimeError("CAS_failed_inside_savepoint")
        except Exception:
            # Savepoint rolled back — proposal stays pending, caller can retry
            logger.warning("command_savepoint_failed", exc_info=True)
            return {
                "error": "command_failed",
                "message": "Command execution failed, please retry.",
            }

        return {
            "status": "completed",
            "proposal_id": proposal.id,
            "result_summary": result_summary,
        }

    async def reject_command(self, business_id: int, proposal_id: str) -> dict[str, Any]:
        """CAS pending -> rejected."""
        proposal = await self._repo.get_by_id(business_id, proposal_id)
        if proposal is None:
            return {"error": "proposal_not_found"}

        if proposal.status != "pending_confirmation":
            return {"error": "invalid_status", "current_status": proposal.status}

        updated = await self._repo.transition_status(
            proposal.id, business_id, proposal.expected_version, "rejected"
        )
        if updated is None:
            return {"error": "transition_failed"}

        return {"status": "rejected", "proposal_id": proposal.id}

    # ===================================================================
    # Internal — owner resolution
    # ===================================================================

    async def _resolve_owner(self, business_id: int, owner_phone: str) -> BusinessUser | None:
        stmt = select(BusinessUser).where(
            BusinessUser.business_id == business_id,
            BusinessUser.phone == owner_phone,
            BusinessUser.role == "owner",
            BusinessUser.is_active.is_(True),
        )
        return (await self._session.scalars(stmt)).first()

    # ===================================================================
    # Internal — stale expiry
    # ===================================================================

    async def _expire_stale(self, business_id: int, owner_user_id: int) -> None:
        """Transition any expired pending proposals so the owner can create new ones."""
        now = utcnow()
        pending = await self._repo.get_latest_for_owner(
            business_id, owner_user_id, statuses=("pending_confirmation",)
        )
        if pending is not None and pending.expires_at <= now:
            await self._repo.transition_status(
                pending.id, business_id, pending.expected_version, "expired"
            )

    # ===================================================================
    # Internal — command parsing (regex-based, no LLM)
    # ===================================================================

    def _parse_command(self, raw_text: str, business_id: int) -> dict[str, Any] | None:
        text = raw_text.strip()

        # close_day: "close tomorrow", "close clinic 2026-08-15"
        m = _CLOSE_RE.match(text)
        if m:
            date_expr = m.group(1).strip()
            target = self._parse_date_expr(date_expr)
            if target is not None:
                return {
                    "command_type": "close_day",
                    "target_date": target,
                    "reason": "Owner requested closure",
                }

        # cancel_appointments: "cancel all tomorrow", "cancel appointments 2026-08-15"
        m = _CANCEL_ALL_RE.match(text)
        if m:
            date_expr = m.group(1).strip()
            target = self._parse_date_expr(date_expr)
            if target is not None:
                return {
                    "command_type": "cancel_appointments",
                    "target_date": target,
                }

        # doctor_leave: "Dr. Priya leave tomorrow"
        m = _DOCTOR_LEAVE_RE.match(text)
        if m:
            doctor_name = m.group(1).strip()
            date_expr = (m.group(2) or "tomorrow").strip()
            target = self._parse_date_expr(date_expr)
            if target is not None:
                return {
                    "command_type": "doctor_leave",
                    "doctor_name": doctor_name,
                    "target_date": target,
                    "reason": "Doctor leave",
                }

        # get_summary (non-destructive, no proposal needed — but parsed here
        # for completeness; the caller can handle it without confirmation)
        m = _GET_SUMMARY_RE.match(text)
        if m:
            return {
                "command_type": "get_summary",
                "target_date": None,
            }

        return None

    @staticmethod
    def _parse_date_expr(expr: str) -> str | None:
        """Return a canonical date string or None if unparseable.

        Returns 'today', 'tomorrow', or an ISO date string.
        """
        lower = expr.lower().strip()
        if lower in _DATE_PATTERNS:
            return _DATE_PATTERNS[lower]
        # Try ISO date
        try:
            date.fromisoformat(lower)
            return lower
        except ValueError:
            return None

    # ===================================================================
    # Internal — resolve target date to concrete date
    # ===================================================================

    async def _resolve_target_date(self, business_id: int, date_expr: str | None) -> date:
        """Convert a date expression to a concrete date using business timezone."""
        tz_name = await self._get_business_timezone(business_id)
        today = datetime.now(ZoneInfo(tz_name)).date()
        if not date_expr:
            return today
        if date_expr == "today":
            return today
        if date_expr == "tomorrow":
            return today + timedelta(days=1)
        try:
            return date.fromisoformat(date_expr)
        except ValueError:
            return today

    async def _get_business_timezone(self, business_id: int) -> str:
        biz = await self._session.scalar(select(Business).where(Business.id == business_id))
        return biz.timezone if biz else "Asia/Kolkata"

    # ===================================================================
    # Internal — query affected entities for preview
    # ===================================================================

    async def _query_affected(self, business_id: int, parsed: dict[str, Any]) -> dict[str, Any]:
        """Build a summary of entities affected by the command."""
        command_type = parsed["command_type"]
        target_date_expr = parsed.get("target_date")
        target_date = await self._resolve_target_date(business_id, target_date_expr)

        if command_type in ("close_day", "cancel_appointments"):
            appointments = await self._query_confirmed_appointments_on_date(
                business_id, target_date
            )
            return {
                "target_date": target_date.isoformat(),
                "confirmed_appointment_count": len(appointments),
                "appointments": [
                    {
                        "id": a.id,
                        "customer_name": a.customer_name or "Patient",
                        "service": a.service_name_snapshot,
                        "resource": a.resource_name_snapshot,
                        "start_at": a.start_at.isoformat(),
                    }
                    for a in appointments
                ],
            }

        if command_type == "doctor_leave":
            doctor_name = parsed.get("doctor_name", "")
            resource = await self._resolve_resource(business_id, doctor_name)
            if resource is None:
                return {
                    "target_date": target_date.isoformat(),
                    "error": f"doctor_not_found: {doctor_name}",
                }
            appointments = await self._query_confirmed_appointments_for_resource(
                business_id, resource.id, target_date
            )
            return {
                "target_date": target_date.isoformat(),
                "resource_id": resource.id,
                "resource_name": resource.name,
                "confirmed_appointment_count": len(appointments),
                "appointments": [
                    {
                        "id": a.id,
                        "customer_name": a.customer_name or "Patient",
                        "service": a.service_name_snapshot,
                        "start_at": a.start_at.isoformat(),
                    }
                    for a in appointments
                ],
            }

        return {}

    # ===================================================================
    # Internal — command execution
    # ===================================================================

    async def _execute_command(
        self,
        business_id: int,
        command_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch to the appropriate command handler."""
        if command_type == "close_day":
            return await self._exec_close_day(business_id, payload)
        if command_type == "cancel_appointments":
            return await self._exec_cancel_appointments(business_id, payload)
        if command_type == "doctor_leave":
            return await self._exec_doctor_leave(business_id, payload)
        raise ValueError(f"unsupported_command_type: {command_type}")

    async def _exec_close_day(self, business_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Upsert a ScheduleException with is_closed=True, then cancel all appointments."""
        target_date_str = payload.get("target_date", "today")
        target_date = await self._resolve_target_date(business_id, target_date_str)
        reason = payload.get("reason", "Owner requested closure")

        # Upsert schedule exception (business-level, no resource)
        await self._upsert_schedule_exception(
            business_id=business_id,
            resource_id=None,
            exception_date=target_date,
            is_closed=True,
            reason=reason,
        )

        # Cancel all confirmed appointments on that date
        cancelled = await self._cancel_confirmed_appointments_on_date(
            business_id, target_date, reason
        )

        return {
            "action": "close_day",
            "target_date": target_date.isoformat(),
            "reason": reason,
            "cancelled_count": len(cancelled),
            "cancelled_appointment_ids": cancelled,
        }

    async def _exec_cancel_appointments(
        self, business_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Cancel all confirmed appointments on the target date (no schedule exception)."""
        target_date_str = payload.get("target_date", "today")
        target_date = await self._resolve_target_date(business_id, target_date_str)

        cancelled = await self._cancel_confirmed_appointments_on_date(
            business_id, target_date, "Owner cancelled all appointments"
        )

        return {
            "action": "cancel_appointments",
            "target_date": target_date.isoformat(),
            "cancelled_count": len(cancelled),
            "cancelled_appointment_ids": cancelled,
        }

    async def _exec_doctor_leave(self, business_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Mark a doctor on leave and cancel their appointments."""
        target_date_str = payload.get("target_date", "today")
        target_date = await self._resolve_target_date(business_id, target_date_str)
        doctor_name = payload.get("doctor_name", "")
        reason = payload.get("reason", "Doctor leave")

        resource = await self._resolve_resource(business_id, doctor_name)
        if resource is None:
            raise ValueError(f"doctor_not_found: {doctor_name}")

        # Upsert resource-level schedule exception
        await self._upsert_schedule_exception(
            business_id=business_id,
            resource_id=resource.id,
            exception_date=target_date,
            is_closed=True,
            reason=reason,
        )

        # Cancel confirmed appointments for this resource on the date
        cancelled = await self._cancel_confirmed_appointments_for_resource(
            business_id, resource.id, target_date, reason
        )

        return {
            "action": "doctor_leave",
            "target_date": target_date.isoformat(),
            "resource_id": resource.id,
            "resource_name": resource.name,
            "reason": reason,
            "cancelled_count": len(cancelled),
            "cancelled_appointment_ids": cancelled,
        }

    # ===================================================================
    # Internal — schedule exception upsert
    # ===================================================================

    async def _upsert_schedule_exception(
        self,
        *,
        business_id: int,
        resource_id: int | None,
        exception_date: date,
        is_closed: bool,
        reason: str,
        open_time: dt_time | None = None,
        close_time: dt_time | None = None,
    ) -> ScheduleException:
        """Insert or detect an existing ScheduleException.

        Checks for an existing exception on the same date and scope.
        If identical, returns it (no-op). If conflicting, raises.
        """
        if resource_id is None:
            stmt = (
                select(ScheduleException)
                .where(
                    ScheduleException.business_id == business_id,
                    ScheduleException.resource_id.is_(None),
                    ScheduleException.exception_date == exception_date,
                )
                .with_for_update()
            )
        else:
            stmt = (
                select(ScheduleException)
                .where(
                    ScheduleException.business_id == business_id,
                    ScheduleException.resource_id == resource_id,
                    ScheduleException.exception_date == exception_date,
                )
                .with_for_update()
            )

        existing = (await self._session.scalars(stmt)).first()

        if existing is not None:
            # Identical exception — no-op
            if existing.is_closed == is_closed and (
                is_closed or (existing.open_time == open_time and existing.close_time == close_time)
            ):
                return existing
            # Conflicting exception
            raise RuntimeError(
                f"schedule_exception_conflict: business_id={business_id} "
                f"resource_id={resource_id} date={exception_date} "
                f"existing_closed={existing.is_closed} requested_closed={is_closed}"
            )

        exc = ScheduleException(
            business_id=business_id,
            resource_id=resource_id,
            exception_date=exception_date,
            is_closed=is_closed,
            open_time=open_time,
            close_time=close_time,
            reason=reason,
        )
        self._session.add(exc)
        await self._session.flush()
        return exc

    # ===================================================================
    # Internal — appointment queries
    # ===================================================================

    async def _query_confirmed_appointments_on_date(
        self, business_id: int, target_date: date
    ) -> list[Appointment]:
        """Return confirmed appointments for a given date in business timezone."""
        tz_name = await self._get_business_timezone(business_id)
        tz = ZoneInfo(tz_name)
        appointments = (
            await self._session.scalars(
                select(Appointment)
                .where(
                    Appointment.business_id == business_id,
                    Appointment.status == AppointmentStatus.CONFIRMED.value,
                )
                .order_by(Appointment.start_at)
            )
        ).all()
        return [a for a in appointments if a.start_at.astimezone(tz).date() == target_date]

    async def _query_confirmed_appointments_for_resource(
        self, business_id: int, resource_id: int, target_date: date
    ) -> list[Appointment]:
        """Return confirmed appointments for a specific resource on a given date."""
        tz_name = await self._get_business_timezone(business_id)
        tz = ZoneInfo(tz_name)
        appointments = (
            await self._session.scalars(
                select(Appointment)
                .where(
                    Appointment.business_id == business_id,
                    Appointment.resource_id == resource_id,
                    Appointment.status == AppointmentStatus.CONFIRMED.value,
                )
                .order_by(Appointment.start_at)
            )
        ).all()
        return [a for a in appointments if a.start_at.astimezone(tz).date() == target_date]

    # ===================================================================
    # Internal — appointment cancellation
    # ===================================================================

    async def _cancel_confirmed_appointments_on_date(
        self, business_id: int, target_date: date, reason: str
    ) -> list[int]:
        """Cancel all confirmed appointments on target_date. Returns cancelled IDs."""
        appointments = await self._query_confirmed_appointments_on_date(business_id, target_date)
        cancelled_ids: list[int] = []
        for appt in appointments:
            await self._cancel_single_appointment(appt, reason)
            cancelled_ids.append(appt.id)
        return cancelled_ids

    async def _cancel_confirmed_appointments_for_resource(
        self, business_id: int, resource_id: int, target_date: date, reason: str
    ) -> list[int]:
        """Cancel confirmed appointments for a resource on target_date."""
        appointments = await self._query_confirmed_appointments_for_resource(
            business_id, resource_id, target_date
        )
        cancelled_ids: list[int] = []
        for appt in appointments:
            await self._cancel_single_appointment(appt, reason)
            cancelled_ids.append(appt.id)
        return cancelled_ids

    async def _cancel_single_appointment(self, appointment: Appointment, reason: str) -> None:
        """Cancel a single appointment and its active resource allocation.

        Also creates cancellation notifications via the NotificationService.
        """
        now = utcnow()

        # Update appointment status
        stmt = (
            update(Appointment)
            .where(
                Appointment.id == appointment.id,
                Appointment.business_id == appointment.business_id,
                Appointment.status == AppointmentStatus.CONFIRMED.value,
            )
            .values(
                status=AppointmentStatus.CANCELLED.value,
                cancelled_at=now,
                reason=reason,
                version=appointment.version + 1,
            )
        )
        await self._session.execute(stmt)

        # Cancel active resource allocation
        stmt = (
            update(ResourceAllocation)
            .where(
                ResourceAllocation.appointment_id == appointment.id,
                ResourceAllocation.business_id == appointment.business_id,
                ResourceAllocation.status == ResourceAllocationStatus.ACTIVE.value,
            )
            .values(
                status=ResourceAllocationStatus.CANCELLED.value,
                version=ResourceAllocation.version + 1,
            )
        )
        await self._session.execute(stmt)

        # Create cancellation notifications
        try:
            from fonely.services.notifications import NotificationService

            notif_service = NotificationService(self._session)
            await notif_service.create_cancellation_notifications(
                business_id=appointment.business_id,
                appointment_id=appointment.id,
                customer_phone=appointment.customer_phone,
                customer_name=appointment.customer_name,
                service_name=appointment.service_name_snapshot,
                resource_name=appointment.resource_name_snapshot,
                start_at=appointment.start_at,
                business_timezone=appointment.business_timezone_snapshot,
                reason=reason,
            )
        except Exception:
            logger.warning(
                "cancellation_notification_failed: appointment_id=%d",
                appointment.id,
                exc_info=True,
            )

    # ===================================================================
    # Internal — resource resolution
    # ===================================================================

    async def _resolve_resource(self, business_id: int, name: str | None) -> Resource | None:
        """Find a resource by partial name match."""
        if not name:
            return None
        name_lower = name.lower()
        resources = (
            await self._session.scalars(
                select(Resource).where(
                    Resource.business_id == business_id,
                    Resource.is_active.is_(True),
                )
            )
        ).all()
        for r in resources:
            if r.name.lower() == name_lower or name_lower in r.name.lower():
                return r
        return None
