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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.validators import utcnow
from fonely.models.enums import AppointmentStatus
from fonely.models.schema import (
    Appointment,
    Business,
    BusinessDailyContext,
    BusinessUser,
    Resource,
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

_CLOSE_EARLY_RE = re.compile(
    r"(?:close)\s+early\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\s*(.*)?",
    re.IGNORECASE,
)
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
    r"(?:show|list|get|view)\s+(?:(tomorrow|today)\s+)?(?:appointments?|summary|schedule)",
    re.IGNORECASE,
)
_ADD_OFFER_RE = re.compile(
    r"(.+?)(?:\s+free|\s+offer|\s+discount|\s+promo)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


_YES_TOKENS = frozenset({"yes", "y", "confirm", "ok", "proceed", "aama", "aam", "sari"})
_NO_TOKENS = frozenset({"no", "n", "cancel", "reject", "stop", "venda", "vendam"})


class OwnerCommandService:
    def __init__(self, session: AsyncSession, model: Any = None) -> None:
        self._session = session
        self._repo = OwnerCommandProposalRepository(session)
        self._model = model

    async def process_command(
        self, business_id: int, owner_phone: str, message: str
    ) -> OwnerCommandResult:
        """Unified entry point for message-driven callers (WhatsApp worker).

        Resolves YES/NO/new-command from message text and pending state.
        Dispatches to preview_command, confirm_command, or reject_command.
        """
        owner = await self._resolve_owner(business_id, owner_phone)
        if owner is None:
            return OwnerCommandResult(
                command_type="error",
                success=False,
                response_text="You are not registered as an active owner.",
            )

        normalised = message.strip().lower()

        if normalised in _YES_TOKENS:
            pending = await self._repo.get_pending_for_owner(business_id, owner.id)
            if not pending:
                return OwnerCommandResult(
                    command_type="confirm",
                    success=False,
                    response_text="No pending command to confirm.",
                )
            if len(pending) != 1:
                return OwnerCommandResult(
                    command_type="confirm",
                    success=False,
                    response_text=(
                        "More than one command is awaiting confirmation. "
                        "Please choose the command explicitly."
                    ),
                )
            proposal = pending[0]
            result = await self.confirm_command(business_id, proposal.id)
            if "error" in result:
                return OwnerCommandResult(
                    command_type=proposal.command_type,
                    success=False,
                    response_text=result.get("message", "Command failed."),
                    proposal_id=result.get("proposal_id"),
                )
            summary = result.get("result_summary") or {}
            affected = (proposal.command_payload or {}).get("affected_summary", {})
            msg = self._format_confirm_message(proposal.command_type, summary, affected, True)
            return OwnerCommandResult(
                command_type=summary.get("action", proposal.command_type),
                success=True,
                response_text=msg,
                affected_appointments=summary.get("cancelled_count", 0),
                proposal_id=result.get("proposal_id"),
            )

        if normalised in _NO_TOKENS:
            pending = await self._repo.get_pending_for_owner(business_id, owner.id)
            if not pending:
                return OwnerCommandResult(
                    command_type="reject",
                    success=False,
                    response_text="No pending command to cancel.",
                )
            if len(pending) != 1:
                return OwnerCommandResult(
                    command_type="reject",
                    success=False,
                    response_text=(
                        "More than one command is awaiting confirmation. "
                        "Please choose the command explicitly."
                    ),
                )
            result = await self.reject_command(business_id, pending[0].id)
            return OwnerCommandResult(
                command_type="reject",
                success=result.get("status") == "rejected",
                response_text=result.get("message", "Command cancelled."),
                proposal_id=result.get("proposal_id"),
            )

        parsed = self._parse_command(message, business_id)
        if parsed is None:
            return OwnerCommandResult(
                command_type="unknown",
                success=False,
                response_text=_UNKNOWN_RESPONSE,
            )

        if parsed["command_type"] == "get_summary":
            return await self._handle_get_summary(business_id, parsed)

        if parsed["command_type"] == "add_offer":
            return await self._handle_add_offer(business_id, parsed)

        result = await self.preview_command(business_id, owner_phone, message)
        if "error" in result:
            return OwnerCommandResult(
                command_type=result.get("command_type", "unknown"),
                success=False,
                response_text=result.get("message", result.get("error", _UNKNOWN_RESPONSE)),
            )
        return OwnerCommandResult(
            command_type=result.get("command_type", "preview"),
            success=True,
            response_text=result.get("message", "Reply YES to confirm or NO to cancel."),
            affected_appointments=result.get("affected_count", 0),
            proposal_id=result.get("proposal_id"),
        )

    # ===================================================================
    # Split API (core logic)
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
        resolved_date = await self._resolve_target_date(business_id, parsed.get("target_date"))
        idem_key = f"owner-cmd-{parsed['command_type']}-{resolved_date.isoformat()}-o{owner.id}-"

        # Check for completed replay
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
            # Could be partial-unique (one pending per owner) or
            # idempotency-key collision from a prior rejected/expired attempt.
            existing_by_key = await self._repo.get_by_idempotency_key(business_id, idem_key)
            if existing_by_key is not None and existing_by_key.status in (
                "rejected",
                "expired",
            ):
                # Prior attempt was non-mutating terminal; create with suffixed key
                retry_key = f"{idem_key}after-{existing_by_key.id}"
                proposal = await self._repo.create_idempotent(
                    {
                        "id": str(uuid.uuid4()),
                        "business_id": business_id,
                        "owner_user_id": owner.id,
                        "command_type": parsed["command_type"],
                        "command_payload": {**parsed, "affected_summary": affected},
                        "status": "pending_confirmation",
                        "idempotency_key": retry_key,
                        "expires_at": utcnow() + timedelta(minutes=10),
                    }
                )
                if proposal is not None:
                    return {
                        "status": "pending_confirmation",
                        "proposal_id": proposal.id,
                        "command_type": proposal.command_type,
                        "command_payload": proposal.command_payload,
                        "expires_at": proposal.expires_at.isoformat(),
                    }
            # Otherwise pending already exists for this owner
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
            return {"error": "proposal_not_found", "message": "No pending command found."}

        if proposal.status != "pending_confirmation":
            return {
                "error": "invalid_status",
                "message": f"Command is already {proposal.status}.",
            }

        now = utcnow()
        if proposal.expires_at <= now:
            await self._repo.transition_status(
                proposal.id, business_id, proposal.expected_version, "expired"
            )
            return {
                "error": "proposal_expired",
                "message": "Command expired. Please send the command again.",
            }

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
        except ValueError as ve:
            logger.warning("command_precondition_failed: %s", ve)
            return {
                "error": "command_precondition_failed",
                "message": str(ve),
            }
        except Exception:
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
            return {"error": "proposal_not_found", "message": "No pending command found."}

        if proposal.status != "pending_confirmation":
            return {
                "error": "invalid_status",
                "message": f"Command is already {proposal.status}.",
            }

        updated = await self._repo.transition_status(
            proposal.id, business_id, proposal.expected_version, "rejected"
        )
        if updated is None:
            return {"error": "transition_failed"}

        return {"status": "rejected", "proposal_id": proposal.id}

    @staticmethod
    def _format_confirm_message(
        command_type: str,
        summary: dict[str, Any],
        affected: dict[str, Any],
        success: bool,
    ) -> str:
        if not success:
            return "Command execution failed, please retry."
        count = summary.get("cancelled_count", 0)
        names = [a.get("customer_name", "Patient") for a in affected.get("appointments", [])]
        if command_type == "doctor_leave":
            resource = summary.get("resource_name", "Doctor")
            parts = [f"{resource} leave confirmed."]
            if count:
                parts.append(f"{count} appointment(s) cancelled")
                if names:
                    parts.append(f"({', '.join(names)})")
            return " ".join(parts)
        if command_type in ("close_day", "close_early", "cancel_appointments"):
            target = summary.get("target_date", "")
            close_time = summary.get("close_time")
            if close_time:
                parts = [f"Clinic closing early at {close_time} on {target}."]
            else:
                parts = [f"Clinic closed on {target}."]
            if count:
                parts.append(f"{count} appointment(s) cancelled")
                if names:
                    parts.append(f"({', '.join(names)})")
            return " ".join(parts)
        return f"Command completed: {command_type}"

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

        # close_early: "close early at 5 PM tomorrow"
        m = _CLOSE_EARLY_RE.match(text)
        if m:
            close_time_str = m.group(1).strip()
            date_expr = (m.group(2) or "today").strip()
            target = self._parse_date_expr(date_expr) if date_expr else "today"
            if target is not None:
                return {
                    "command_type": "close_early",
                    "close_time": close_time_str,
                    "target_date": target,
                    "reason": f"Owner requested early closure at {close_time_str}",
                }

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

        # get_summary (non-destructive, no proposal needed)
        m = _GET_SUMMARY_RE.match(text)
        if m:
            return {
                "command_type": "get_summary",
                "target_date": m.group(1) or None,
            }

        # add_offer (non-destructive, no proposal needed)
        m = _ADD_OFFER_RE.search(text)
        if m:
            return {
                "command_type": "add_offer",
                "description": text,
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

        if command_type in ("close_day", "close_early", "cancel_appointments"):
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
    # Non-destructive commands (bypass proposal system)
    # ===================================================================

    async def _handle_get_summary(
        self, business_id: int, parsed: dict[str, Any]
    ) -> OwnerCommandResult:
        target_date = await self._resolve_target_date(business_id, parsed.get("target_date"))
        appointments = await self._query_confirmed_appointments_on_date(business_id, target_date)
        tz_name = await self._get_business_timezone(business_id)
        tz = ZoneInfo(tz_name)
        count = len(appointments)
        if count == 0:
            text = f"No appointments on {target_date.isoformat()}."
        else:
            lines = []
            for a in appointments:
                local_time = a.start_at.astimezone(tz).strftime("%-I:%M %p")
                name = a.customer_name or "Patient"
                lines.append(f"  {local_time} — {name} ({a.service_name_snapshot})")
            text = (
                f"{count} appointment{'s' if count != 1 else ''} on "
                f"{target_date.isoformat()}:\n" + "\n".join(lines)
            )
        return OwnerCommandResult(
            command_type="get_summary",
            success=True,
            response_text=text,
        )

    async def _handle_add_offer(
        self, business_id: int, parsed: dict[str, Any]
    ) -> OwnerCommandResult:
        description = parsed.get("description", "")
        tz_name = await self._get_business_timezone(business_id)
        today = datetime.now(ZoneInfo(tz_name)).date()
        context = BusinessDailyContext(
            business_id=business_id,
            context_date=today,
            context_type="offer",
            content=description,
            active=True,
        )
        self._session.add(context)
        await self._session.flush()
        return OwnerCommandResult(
            command_type="add_offer",
            success=True,
            response_text=f"Offer added: {description}",
        )

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
        if command_type == "close_early":
            return await self._exec_close_early(business_id, payload)
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

    async def _exec_close_early(self, business_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Override close_time for the target date, cancel appointments after it."""
        target_date_str = payload.get("target_date", "today")
        target_date = await self._resolve_target_date(business_id, target_date_str)
        close_time_str = payload.get("close_time", "17:00")
        reason = payload.get("reason", f"Owner requested early closure at {close_time_str}")

        close_time = self._parse_time(close_time_str)

        from fonely.models.schema import OperatingSchedule

        dow = target_date.weekday()
        sched = (
            await self._session.scalars(
                select(OperatingSchedule).where(
                    OperatingSchedule.business_id == business_id,
                    OperatingSchedule.day_of_week == dow,
                    OperatingSchedule.is_active.is_(True),
                )
            )
        ).first()
        if sched is None or sched.open_time is None:
            raise ValueError(
                f"Cannot close early: no operating schedule found for "
                f"{target_date.strftime('%A')}. Please set business hours first."
            )
        open_time = sched.open_time

        await self._upsert_schedule_exception(
            business_id=business_id,
            resource_id=None,
            exception_date=target_date,
            is_closed=False,
            reason=reason,
            open_time=open_time,
            close_time=close_time,
        )

        tz_name = await self._get_business_timezone(business_id)
        tz = ZoneInfo(tz_name)
        all_appts = await self._query_confirmed_appointments_on_date(business_id, target_date)
        cancelled_ids: list[int] = []
        for appt in all_appts:
            local_time = appt.start_at.astimezone(tz).time()
            if local_time >= close_time:
                await self._cancel_single_appointment(appt, reason)
                cancelled_ids.append(appt.id)

        return {
            "action": "close_early",
            "target_date": target_date.isoformat(),
            "close_time": close_time_str,
            "reason": reason,
            "cancelled_count": len(cancelled_ids),
            "cancelled_appointment_ids": cancelled_ids,
        }

    @staticmethod
    def _parse_time(time_str: str) -> dt_time:
        s = time_str.strip().upper()
        for fmt in ("%I:%M %p", "%I %p", "%H:%M", "%H"):
            try:
                return datetime.strptime(s, fmt).time()
            except ValueError:
                continue
        return dt_time(17, 0)

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
        """Cancel via AppointmentService to satisfy commit-provenance trigger."""
        import uuid as _uuid

        from fonely.api.internal.validation import InternalValidationPort
        from fonely.domain.appointments.commands import (
            ConfirmPendingAppointmentCancellationCommand,
            CreatePendingAppointmentCancellationCommand,
        )
        from fonely.domain.appointments.errors import (
            AppointmentDomainError,
            AppointmentErrorCode,
        )
        from fonely.domain.pending_actions.commands import ActorContext
        from fonely.models.enums import CallerRole
        from fonely.services.appointments import AppointmentService

        biz = await self._session.scalar(
            select(Business).where(Business.id == appointment.business_id)
        )
        owner_phone = biz.primary_contact_phone if biz else "+910000000000"
        actor = ActorContext(
            business_id=appointment.business_id,
            normalized_phone=owner_phone,
            verified_role=CallerRole.OWNER,
        )
        now = utcnow()
        key = f"owner-cancel-{appointment.id}-{_uuid.uuid4().hex[:8]}"
        appt_service = AppointmentService(
            self._session, validation=InternalValidationPort(self._session)
        )
        try:
            proposal = await appt_service.create_cancellation_proposal(
                CreatePendingAppointmentCancellationCommand(
                    actor=actor,
                    appointment_id=appointment.id,
                    expected_appointment_version=appointment.version,
                    reason_code="owner_command",
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
        except AppointmentDomainError as exc:
            if exc.code in (
                AppointmentErrorCode.INVALID_STATE,
                AppointmentErrorCode.STALE_VERSION,
            ):
                reloaded = (
                    await self._session.scalars(
                        select(Appointment).where(
                            Appointment.id == appointment.id,
                            Appointment.business_id == appointment.business_id,
                        )
                    )
                ).first()
                if reloaded is not None and reloaded.status == "cancelled":
                    return
            logger.warning(
                "cancellation_via_service_failed: appointment_id=%d",
                appointment.id,
                exc_info=True,
            )
            raise

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
