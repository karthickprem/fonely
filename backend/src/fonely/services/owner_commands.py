"""Owner command execution service with durable two-phase confirmation.

Non-destructive commands (get_summary, add_offer, add_note) execute immediately.
Destructive commands (doctor_leave, close_clinic, close_early) go through:

  Phase 1 — Preview: parse, lock, compute affected appointments, persist
            OwnerCommandProposal with preview_snapshot + payload_digest,
            return human-readable preview.
  Phase 2 — Confirm: on bare YES/confirm, load pending proposal FOR UPDATE,
            verify expiry + owner identity, CAS to 'executing', recompute
            targets under fresh locks, detect drift, execute cancellations,
            persist evidence, CAS to 'completed'.
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.appointments.availability import (
    LocalShift,
    TimeWindow,
    can_encode_as_single_interval,
    fits_one_shift,
    normalize_local_shifts,
    schedule_weekday,
    truncate_shifts_at,
)
from fonely.models.enums import CallerRole, DailyContextType
from fonely.models.schema import (
    Appointment,
    Business,
    BusinessDailyContext,
    BusinessUser,
    Resource,
    ScheduleException,
)
from fonely.repositories.appointments import AppointmentRepository
from fonely.repositories.owner_command_proposals import (
    OwnerCommandProposalRepository,
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

_YES_TOKENS = frozenset({"yes", "y", "confirm", "ok", "proceed", "aama", "aam", "sari"})
_NO_TOKENS = frozenset({"no", "n", "cancel", "reject", "stop", "venda", "vendam"})

_DESTRUCTIVE_COMMANDS = frozenset({"doctor_leave", "close_clinic", "close_early"})

_PROPOSAL_TTL = timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerCommandResult:
    command_type: str
    success: bool
    response_text: str
    affected_appointments: int = 0
    affected_patients: int = 0
    details: list[str] = field(default_factory=list)
    proposal_id: str | None = None


# ---------------------------------------------------------------------------
# Evidence model
# ---------------------------------------------------------------------------


class OwnerCommandOutcomeEvidence(BaseModel):
    """Typed evidence attached to a completed proposal."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    outcome: Literal["completed", "completed_with_drift", "rejected", "expired"]
    command_type: str
    target_date: str
    resource_name: str | None = None
    preview_count: int = Field(ge=0)
    confirm_count: int = Field(ge=0)
    cancelled_count: int = Field(ge=0)
    drift_detected: bool = False
    drift_details: list[str] = Field(default_factory=list)
    cancelled_appointments: list[dict[str, str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class OwnerCommandService:
    def __init__(self, session: AsyncSession, model: ModelGateway) -> None:
        self._session = session
        self._parser = OwnerCommandParser(model)
        self._appointments = AppointmentRepository(session)
        self._proposals = OwnerCommandProposalRepository(session)

    # -----------------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------------

    async def process_command(
        self, business_id: int, owner_phone: str, message: str
    ) -> OwnerCommandResult:
        owner = await self._require_active_owner(business_id, owner_phone)
        if owner is None:
            return OwnerCommandResult(
                command_type="error",
                success=False,
                response_text="You are not registered as an active owner for this business.",
            )

        normalised = message.strip().lower()

        # Check for bare YES/NO against a pending proposal
        if normalised in _YES_TOKENS:
            return await self._handle_confirm(business_id, owner)

        if normalised in _NO_TOKENS:
            return await self._handle_reject(business_id, owner)

        # Parse the message as a new command
        doctor_names = await self._get_doctor_names(business_id)
        parsed = await self._parser.parse(message, doctor_names)

        if parsed.command in _DESTRUCTIVE_COMMANDS:
            return await self._handle_destructive_preview(business_id, owner, parsed)

        # Non-destructive commands execute immediately
        if parsed.command == "get_summary":
            return await self._handle_get_summary(business_id, parsed)
        if parsed.command == "add_offer":
            return await self._handle_add_context(
                business_id, owner.phone, parsed, DailyContextType.OFFER
            )
        if parsed.command == "add_note":
            return await self._handle_add_context(
                business_id, owner.phone, parsed, DailyContextType.NOTE
            )

        return OwnerCommandResult(
            command_type="unknown",
            success=False,
            response_text=_UNKNOWN_RESPONSE,
        )

    # -----------------------------------------------------------------------
    # Phase 1 — Destructive preview
    # -----------------------------------------------------------------------

    async def _handle_destructive_preview(
        self,
        business_id: int,
        owner: BusinessUser,
        parsed: ParsedOwnerCommand,
    ) -> OwnerCommandResult:
        """Build preview, persist proposal, return confirmation prompt."""

        target_date = self._resolve_date_from_parsed(
            await self._get_business_timezone(business_id), parsed
        )
        if target_date is None:
            return OwnerCommandResult(
                command_type=parsed.command,
                success=False,
                response_text=(
                    "Unsupported date expression. Please use 'today', 'tomorrow', or YYYY-MM-DD."
                ),
            )

        # Command-specific validation and preview building
        if parsed.command == "doctor_leave":
            return await self._preview_doctor_leave(business_id, owner, parsed, target_date)
        if parsed.command == "close_clinic":
            return await self._preview_close_clinic(business_id, owner, parsed, target_date)
        if parsed.command == "close_early":
            return await self._preview_close_early(business_id, owner, parsed, target_date)

        # Should not reach here, but guard
        return OwnerCommandResult(
            command_type=parsed.command,
            success=False,
            response_text="Internal error: unrecognised destructive command.",
        )

    async def _preview_doctor_leave(
        self,
        business_id: int,
        owner: BusinessUser,
        parsed: ParsedOwnerCommand,
        target_date: date,
    ) -> OwnerCommandResult:
        resource = await self._resolve_resource(business_id, parsed.doctor_name)
        if resource is None:
            names = await self._get_doctor_names(business_id)
            return OwnerCommandResult(
                command_type="doctor_leave",
                success=False,
                response_text=(
                    f"Could not find doctor '{parsed.doctor_name}'. Available: {', '.join(names)}"
                ),
            )

        await self._appointments.lock_resource_schedule(business_id, resource.id)
        affected = await self._query_resource_appointments(business_id, resource.id, target_date)
        preview = self._build_preview_snapshot(
            parsed.command, target_date, affected, resource_name=resource.name
        )
        payload = {
            "command_type": "doctor_leave",
            "target_date": target_date.isoformat(),
            "resource_id": resource.id,
            "resource_name": resource.name,
            "reason": parsed.reason or "Leave",
        }
        return await self._persist_preview(
            business_id, owner, parsed.command, payload, preview, affected, target_date
        )

    async def _preview_close_clinic(
        self,
        business_id: int,
        owner: BusinessUser,
        parsed: ParsedOwnerCommand,
        target_date: date,
    ) -> OwnerCommandResult:
        await self._lock_business_resources(business_id)
        affected = await self._query_all_appointments(business_id, target_date)
        preview = self._build_preview_snapshot(parsed.command, target_date, affected)
        payload = {
            "command_type": "close_clinic",
            "target_date": target_date.isoformat(),
            "reason": parsed.reason or "Closed",
        }
        return await self._persist_preview(
            business_id, owner, parsed.command, payload, preview, affected, target_date
        )

    async def _preview_close_early(
        self,
        business_id: int,
        owner: BusinessUser,
        parsed: ParsedOwnerCommand,
        target_date: date,
    ) -> OwnerCommandResult:
        from fonely.models.schema import OperatingSchedule

        if not parsed.close_time:
            return OwnerCommandResult(
                command_type="close_early",
                success=False,
                response_text="Please specify a close time, e.g. 'Close early at 6 PM'.",
            )

        try:
            parts = parsed.close_time.split(":")
            new_close = dt_time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except (ValueError, IndexError):
            return OwnerCommandResult(
                command_type="close_early",
                success=False,
                response_text=f"Could not understand the time '{parsed.close_time}'.",
            )

        await self._lock_business_resources(business_id)

        day_of_week = schedule_weekday(target_date)
        schedules = (
            (
                await self._session.execute(
                    select(OperatingSchedule).where(
                        OperatingSchedule.business_id == business_id,
                        OperatingSchedule.day_of_week == day_of_week,
                        OperatingSchedule.is_active.is_(True),
                        OperatingSchedule.resource_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        weekly_shifts = normalize_local_shifts(
            tuple(LocalShift(s.open_time, s.close_time) for s in schedules)
        )
        if not weekly_shifts:
            return OwnerCommandResult(
                command_type="close_early",
                success=False,
                response_text="No schedule found for this day.",
            )

        earliest_open = weekly_shifts[0].open_time
        latest_close = weekly_shifts[-1].close_time

        if new_close <= earliest_open:
            return OwnerCommandResult(
                command_type="close_early",
                success=False,
                response_text="Close time must be after the opening time.",
            )
        if new_close >= latest_close:
            return OwnerCommandResult(
                command_type="close_early",
                success=False,
                response_text="Close time must be before the current closing time.",
            )

        truncated = truncate_shifts_at(weekly_shifts, new_close)
        if not can_encode_as_single_interval(truncated):
            return OwnerCommandResult(
                command_type="close_early",
                success=False,
                response_text=(
                    "Cannot close early at this time because the schedule has "
                    "multiple shifts with gaps. Please close at a time that "
                    "does not span a gap, or close the clinic entirely."
                ),
            )

        # Query appointments that would fall outside the truncated schedule
        affected = await self._query_appointments_outside_schedule(business_id, target_date)
        preview = self._build_preview_snapshot(parsed.command, target_date, affected)
        payload: dict[str, Any] = {
            "command_type": "close_early",
            "target_date": target_date.isoformat(),
            "close_time": parsed.close_time,
            "reason": parsed.reason or "Closing early",
        }
        return await self._persist_preview(
            business_id, owner, parsed.command, payload, preview, affected, target_date
        )

    # -----------------------------------------------------------------------
    # Preview persistence
    # -----------------------------------------------------------------------

    def _build_preview_snapshot(
        self,
        command_type: str,
        target_date: date,
        affected: list[dict[str, str]],
        *,
        resource_name: str | None = None,
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "command_type": command_type,
            "target_date": target_date.isoformat(),
            "affected_count": len(affected),
            "appointments": [
                {
                    "time": a["time"],
                    "patient": a["patient"],
                    "service": a.get("service", ""),
                    "phone": a.get("phone", ""),
                    "resource_name": a.get("resource_name", ""),
                    "appointment_id": a.get("appointment_id", ""),
                }
                for a in affected
            ],
        }
        if resource_name:
            snapshot["resource_name"] = resource_name
        return snapshot

    @staticmethod
    def _compute_digest(preview_snapshot: dict[str, Any]) -> str:
        canonical = json.dumps(preview_snapshot, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def _persist_preview(
        self,
        business_id: int,
        owner: BusinessUser,
        command_type: str,
        payload: dict[str, Any],
        preview: dict[str, Any],
        affected: list[dict[str, str]],
        target_date: date,
    ) -> OwnerCommandResult:
        digest = self._compute_digest(preview)
        idem_key = f"owner-v1-{business_id}-{owner.id}-{digest[:40]}"
        now = datetime.now(UTC)

        proposal = await self._proposals.create_idempotent(
            {
                "id": uuid.uuid4().hex,
                "business_id": business_id,
                "owner_user_id": owner.id,
                "owner_phone_snapshot": owner.phone,
                "command_type": command_type,
                "command_payload": payload,
                "preview_snapshot": preview,
                "payload_digest": digest,
                "status": "pending_confirmation",
                "expected_version": 1,
                "idempotency_key": idem_key,
                "expires_at": now + _PROPOSAL_TTL,
            }
        )

        if proposal is None:
            # A pending proposal already exists for this owner
            existing = await self._proposals.get_latest_for_owner(business_id, owner.id)
            if existing is not None:
                return OwnerCommandResult(
                    command_type=command_type,
                    success=False,
                    response_text=(
                        "You already have a pending command awaiting confirmation. "
                        "Reply YES to confirm or NO to cancel it first."
                    ),
                    proposal_id=existing.id,
                )
            return OwnerCommandResult(
                command_type=command_type,
                success=False,
                response_text="Could not create command proposal. Please try again.",
            )

        # Build preview text
        text = self._format_preview_text(command_type, target_date, affected, payload)
        return OwnerCommandResult(
            command_type=command_type,
            success=True,
            response_text=text,
            affected_appointments=len(affected),
            affected_patients=len(affected),
            proposal_id=proposal.id,
        )

    def _format_preview_text(
        self,
        command_type: str,
        target_date: date,
        affected: list[dict[str, str]],
        payload: dict[str, Any],
    ) -> str:
        date_str = target_date.strftime("%b %d")
        lines: list[str] = []

        if command_type == "doctor_leave":
            name = payload.get("resource_name", "Doctor")
            lines.append(f"Mark {name} on leave for {date_str}?")
        elif command_type == "close_clinic":
            reason = payload.get("reason", "Closed")
            lines.append(f"Close clinic on {date_str}? ({reason})")
        elif command_type == "close_early":
            close_time = payload.get("close_time", "")
            lines.append(f"Close clinic early at {close_time} on {date_str}?")

        if affected:
            lines.append(f"\n{len(affected)} appointment(s) will be cancelled:")
            for appt in affected:
                patient = appt.get("patient", "Patient")
                time_str = appt.get("time", "")
                service = appt.get("service", "")
                detail = f"  - {time_str} {patient}"
                if service:
                    detail += f" ({service})"
                lines.append(detail)
            lines.append("\nAll affected patients will be notified.")
        else:
            lines.append("\nNo appointments will be affected.")

        lines.append("\nReply YES to confirm or NO to cancel.")
        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Phase 2 — Confirm
    # -----------------------------------------------------------------------

    async def _handle_confirm(self, business_id: int, owner: BusinessUser) -> OwnerCommandResult:
        now = datetime.now(UTC)
        proposal = await self._proposals.get_latest_for_owner(
            business_id, owner.id, statuses=("pending_confirmation",)
        )
        if proposal is None:
            return OwnerCommandResult(
                command_type="confirm",
                success=False,
                response_text="No pending command to confirm.",
            )

        if proposal.expires_at <= now:
            await self._proposals.transition_status(
                proposal.id,
                business_id,
                proposal.expected_version,
                "expired",
            )
            return OwnerCommandResult(
                command_type="confirm",
                success=False,
                response_text="That command has expired. Please send the command again.",
            )

        # CAS to 'executing'
        executing = await self._proposals.transition_status(
            proposal.id,
            business_id,
            proposal.expected_version,
            "executing",
            require_unexpired_at=now,
            confirmed_at=now,
        )
        if executing is None:
            return OwnerCommandResult(
                command_type="confirm",
                success=False,
                response_text=(
                    "Could not confirm — the command may have been "
                    "modified or expired. Please try again."
                ),
            )

        # Execute under fresh locks
        try:
            result = await self._execute_confirmed(business_id, owner, executing)
        except Exception:
            logger.exception("owner_command_execution_failed proposal_id=%s", proposal.id)
            await self._proposals.transition_status(
                proposal.id,
                business_id,
                executing.expected_version,
                "failed",
                failure_code="execution_error",
                failure_message="Unexpected error during execution",
            )
            return OwnerCommandResult(
                command_type=proposal.command_type,
                success=False,
                response_text="An error occurred while executing the command. Please try again.",
            )

        return result

    async def _execute_confirmed(
        self,
        business_id: int,
        owner: BusinessUser,
        proposal: Any,
    ) -> OwnerCommandResult:
        """Execute the confirmed destructive command with drift detection."""
        payload = proposal.command_payload
        command_type = proposal.command_type
        target_date = date.fromisoformat(payload["target_date"])

        preview_appointments = proposal.preview_snapshot.get("appointments", [])
        preview_count = len(preview_appointments)

        # Recompute targets under fresh locks
        current_targets = await self._targets_at_confirmation(business_id, command_type, payload)

        # Detect drift
        drift_detected = False
        drift_details: list[str] = []
        if len(current_targets) != preview_count:
            drift_detected = True
            drift_details.append(
                f"Appointment count changed: {preview_count} at preview, {len(current_targets)} now"
            )

        # Check individual appointment IDs if available
        preview_ids = {
            str(a.get("appointment_id", ""))
            for a in preview_appointments
            if a.get("appointment_id")
        }
        current_ids = {
            str(a.get("appointment_id", "")) for a in current_targets if a.get("appointment_id")
        }
        if preview_ids and current_ids:
            added = current_ids - preview_ids
            removed = preview_ids - current_ids
            if added:
                drift_detected = True
                drift_details.append(f"New appointments added: {len(added)}")
            if removed:
                drift_detected = True
                drift_details.append(f"Appointments removed since preview: {len(removed)}")

        # Execute the actual command
        if command_type == "doctor_leave":
            cancelled = await self._execute_doctor_leave(
                business_id, owner.phone, payload, target_date
            )
        elif command_type == "close_clinic":
            cancelled = await self._execute_close_clinic(
                business_id, owner.phone, payload, target_date
            )
        elif command_type == "close_early":
            cancelled = await self._execute_close_early(
                business_id, owner.phone, payload, target_date
            )
        else:
            cancelled = []

        # Build evidence
        outcome: Literal["completed", "completed_with_drift"] = (
            "completed_with_drift" if drift_detected else "completed"
        )
        evidence = OwnerCommandOutcomeEvidence(
            outcome=outcome,
            command_type=command_type,
            target_date=target_date.isoformat(),
            resource_name=payload.get("resource_name"),
            preview_count=preview_count,
            confirm_count=len(current_targets),
            cancelled_count=len(cancelled),
            drift_detected=drift_detected,
            drift_details=drift_details,
            cancelled_appointments=cancelled,
        )

        # Transition to completed
        now = datetime.now(UTC)
        await self._proposals.transition_status(
            proposal.id,
            business_id,
            proposal.expected_version,
            "completed",
            result_evidence=evidence.model_dump(mode="json"),
            completed_at=now,
        )

        # Format completion text
        text = self._format_completion_text(
            command_type, target_date, payload, cancelled, drift_detected, drift_details
        )
        return OwnerCommandResult(
            command_type=command_type,
            success=True,
            response_text=text,
            affected_appointments=len(cancelled),
            affected_patients=len(cancelled),
            details=[c.get("patient", "") for c in cancelled],
            proposal_id=proposal.id,
        )

    def _format_completion_text(
        self,
        command_type: str,
        target_date: date,
        payload: dict[str, Any],
        cancelled: list[dict[str, str]],
        drift_detected: bool,
        drift_details: list[str],
    ) -> str:
        date_str = target_date.strftime("%b %d")
        lines: list[str] = []

        if command_type == "doctor_leave":
            name = payload.get("resource_name", "Doctor")
            lines.append(f"Done. {name} marked on leave for {date_str}.")
            if cancelled:
                lines.append(f"{len(cancelled)} appointment(s) cancelled:")
                for appt in cancelled:
                    lines.append(
                        f"  - {appt['time']} {appt['patient']}"
                        f" ({appt.get('service', '')}) — notified"
                    )
                lines.append(f"{name} will not be booked for {date_str}.")
            else:
                lines.append(f"No appointments to cancel. {name} will not be booked.")

        elif command_type == "close_clinic":
            reason = payload.get("reason", "Closed")
            lines.append(f"Clinic closed on {date_str}. {reason}.")
            if cancelled:
                lines.append(f"{len(cancelled)} appointment(s) cancelled and patients notified.")

        elif command_type == "close_early":
            close_time_str = payload.get("close_time", "")
            try:
                parts = close_time_str.split(":")
                ct = dt_time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
                close_display = ct.strftime("%-I:%M %p")
            except (ValueError, IndexError):
                close_display = close_time_str
            lines.append(f"Clinic closing early at {close_display} on {date_str}.")
            if cancelled:
                lines.append(f"{len(cancelled)} appointment(s) after {close_display} cancelled:")
                for appt in cancelled:
                    lines.append(f"  - {appt['time']} {appt['patient']} — notified")
            else:
                lines.append("No appointments affected.")

        if drift_detected:
            lines.append("\nNote: Appointments changed between preview and confirmation:")
            for detail in drift_details:
                lines.append(f"  * {detail}")

        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Phase 2 — Reject
    # -----------------------------------------------------------------------

    async def _handle_reject(self, business_id: int, owner: BusinessUser) -> OwnerCommandResult:
        proposal = await self._proposals.get_latest_for_owner(
            business_id, owner.id, statuses=("pending_confirmation",)
        )
        if proposal is None:
            return OwnerCommandResult(
                command_type="reject",
                success=False,
                response_text="No pending command to cancel.",
            )

        rejected = await self._proposals.transition_status(
            proposal.id,
            business_id,
            proposal.expected_version,
            "rejected",
            result_evidence={
                "outcome": "rejected",
                "command_type": proposal.command_type,
            },
        )
        if rejected is None:
            return OwnerCommandResult(
                command_type="reject",
                success=False,
                response_text="Could not cancel the command. It may have already been processed.",
            )

        return OwnerCommandResult(
            command_type="reject",
            success=True,
            response_text="Command cancelled. No changes were made.",
            proposal_id=proposal.id,
        )

    # -----------------------------------------------------------------------
    # Execution helpers (doctor_leave, close_clinic, close_early)
    # -----------------------------------------------------------------------

    async def _execute_doctor_leave(
        self,
        business_id: int,
        owner_phone: str,
        payload: dict[str, Any],
        target_date: date,
    ) -> list[dict[str, str]]:
        resource_id: int = payload["resource_id"]
        reason: str = payload.get("reason", "Leave")

        await self._appointments.lock_resource_schedule(business_id, resource_id)

        exc = ScheduleException(
            business_id=business_id,
            resource_id=resource_id,
            exception_date=target_date,
            is_closed=True,
            reason=reason,
        )
        self._session.add(exc)
        await self._session.flush()

        return await self._cancel_appointments_for_resource(
            business_id, resource_id, target_date, owner_phone
        )

    async def _execute_close_clinic(
        self,
        business_id: int,
        owner_phone: str,
        payload: dict[str, Any],
        target_date: date,
    ) -> list[dict[str, str]]:
        reason: str = payload.get("reason", "Closed")

        await self._lock_business_resources(business_id)

        exc = ScheduleException(
            business_id=business_id,
            resource_id=None,
            exception_date=target_date,
            is_closed=True,
            reason=reason,
        )
        self._session.add(exc)
        await self._session.flush()

        return await self._cancel_all_appointments(business_id, target_date, owner_phone)

    async def _execute_close_early(
        self,
        business_id: int,
        owner_phone: str,
        payload: dict[str, Any],
        target_date: date,
    ) -> list[dict[str, str]]:
        from fonely.models.schema import OperatingSchedule

        close_time_str: str = payload["close_time"]
        reason: str = payload.get("reason", "Closing early")

        parts = close_time_str.split(":")
        new_close = dt_time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)

        await self._lock_business_resources(business_id)

        day_of_week = schedule_weekday(target_date)
        schedules = (
            (
                await self._session.execute(
                    select(OperatingSchedule).where(
                        OperatingSchedule.business_id == business_id,
                        OperatingSchedule.day_of_week == day_of_week,
                        OperatingSchedule.is_active.is_(True),
                        OperatingSchedule.resource_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        weekly_shifts = normalize_local_shifts(
            tuple(LocalShift(s.open_time, s.close_time) for s in schedules)
        )
        truncated = truncate_shifts_at(weekly_shifts, new_close)

        if truncated:
            effective = truncated[0]
            exc = ScheduleException(
                business_id=business_id,
                resource_id=None,
                exception_date=target_date,
                is_closed=False,
                open_time=effective.open_time,
                close_time=effective.close_time,
                reason=reason,
            )
        else:
            exc = ScheduleException(
                business_id=business_id,
                resource_id=None,
                exception_date=target_date,
                is_closed=True,
                reason=reason,
            )
        self._session.add(exc)
        await self._session.flush()

        return await self._cancel_appointments_outside_schedule(
            business_id, target_date, owner_phone
        )

    # -----------------------------------------------------------------------
    # Targets at confirmation (recompute under fresh locks)
    # -----------------------------------------------------------------------

    async def _targets_at_confirmation(
        self,
        business_id: int,
        command_type: str,
        payload: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Recompute affected appointments under fresh locks for drift detection."""
        target_date = date.fromisoformat(payload["target_date"])

        if command_type == "doctor_leave":
            resource_id: int = payload["resource_id"]
            await self._appointments.lock_resource_schedule(business_id, resource_id)
            return await self._query_resource_appointments(business_id, resource_id, target_date)
        elif command_type == "close_clinic":
            await self._lock_business_resources(business_id)
            return await self._query_all_appointments(business_id, target_date)
        elif command_type == "close_early":
            await self._lock_business_resources(business_id)
            return await self._query_appointments_outside_schedule(business_id, target_date)
        return []

    # -----------------------------------------------------------------------
    # Owner identity
    # -----------------------------------------------------------------------

    async def _require_active_owner(
        self, business_id: int, owner_phone: str
    ) -> BusinessUser | None:
        """Find exactly one active owner for the business matching the phone."""
        result = await self._session.execute(
            select(BusinessUser).where(
                BusinessUser.business_id == business_id,
                BusinessUser.phone == owner_phone,
                BusinessUser.role == "owner",
                BusinessUser.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    # -----------------------------------------------------------------------
    # Date resolution
    # -----------------------------------------------------------------------

    def _resolve_date_from_parsed(self, timezone: str, parsed: ParsedOwnerCommand) -> date | None:
        """Resolve parsed date expression to a concrete date. Returns None for unsupported."""
        expr = parsed.date
        today = datetime.now(ZoneInfo(timezone)).date()
        if not expr:
            return today
        expr_lower = expr.lower().strip()
        if expr_lower in ("today", "innikku", "இன்று"):
            return today
        if expr_lower in ("tomorrow", "naalaikku", "நாளை"):
            return today + timedelta(days=1)
        try:
            return date.fromisoformat(expr_lower)
        except ValueError:
            return None

    async def _resolve_date(self, business_id: int, expr: str | None) -> date:
        """Legacy date resolver for non-destructive commands — falls back to today."""
        timezone = await self._get_business_timezone(business_id)
        today = datetime.now(ZoneInfo(timezone)).date()
        if not expr:
            return today
        expr_lower = expr.lower().strip()
        if expr_lower in ("today", "innikku", "இன்று"):
            return today
        if expr_lower in ("tomorrow", "naalaikku", "நாளை"):
            return today + timedelta(days=1)
        try:
            return date.fromisoformat(expr_lower)
        except ValueError:
            return today

    # -----------------------------------------------------------------------
    # Resource resolution
    # -----------------------------------------------------------------------

    async def _resolve_resource(self, business_id: int, name: str | None) -> Resource | None:
        """Require exact or unique partial match on resource name."""
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
        # Exact match first
        for r in resources:
            if r.name.lower() == name_lower:
                return r
        # Unique partial match
        partials = [r for r in resources if name_lower in r.name.lower()]
        if len(partials) == 1:
            return partials[0]
        return None

    # -----------------------------------------------------------------------
    # Appointment queries (for preview and drift detection)
    # -----------------------------------------------------------------------

    async def _query_resource_appointments(
        self,
        business_id: int,
        resource_id: int,
        target_date: date,
    ) -> list[dict[str, str]]:
        """Query confirmed appointments for a resource on a date."""
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
        result: list[dict[str, str]] = []
        for appt in appointments:
            if appt.start_at.astimezone(tz).date() != target_date:
                continue
            local_time = appt.start_at.astimezone(tz).strftime("%-I:%M %p")
            result.append(
                {
                    "appointment_id": str(appt.id),
                    "time": local_time,
                    "patient": appt.customer_name or "Patient",
                    "service": appt.service_name_snapshot,
                    "phone": appt.customer_phone,
                    "resource_name": appt.resource_name_snapshot,
                }
            )
        return result

    async def _query_all_appointments(
        self,
        business_id: int,
        target_date: date,
    ) -> list[dict[str, str]]:
        """Query all confirmed appointments for a business on a date."""
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
        result: list[dict[str, str]] = []
        for appt in appointments:
            if appt.start_at.astimezone(tz).date() != target_date:
                continue
            result.append(
                {
                    "appointment_id": str(appt.id),
                    "time": appt.start_at.astimezone(tz).strftime("%-I:%M %p"),
                    "patient": appt.customer_name or "Patient",
                    "service": appt.service_name_snapshot,
                    "phone": appt.customer_phone,
                    "resource_name": appt.resource_name_snapshot,
                }
            )
        return result

    async def _query_appointments_outside_schedule(
        self,
        business_id: int,
        target_date: date,
    ) -> list[dict[str, str]]:
        """Query confirmed appointments that fall outside truncated schedule."""
        from fonely.services.availability import AvailabilityService

        timezone = await self._get_business_timezone(business_id)
        zone = ZoneInfo(timezone)
        svc = AvailabilityService(self._session)
        shift_windows = await svc._get_shift_windows(business_id, 0, target_date, timezone)
        all_resource_ids = await self._appointments.list_active_resource_ids(business_id)
        resource_shift_cache: dict[int, list[TimeWindow]] = {}
        for rid in all_resource_ids:
            resource_shift_cache[rid] = await svc._get_shift_windows(
                business_id, rid, target_date, timezone
            )

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

        result: list[dict[str, str]] = []
        for appointment in appointments:
            local_start = appointment.start_at.astimezone(zone)
            if local_start.date() != target_date:
                continue
            effective = TimeWindow(
                appointment.effective_start_at or appointment.start_at,
                appointment.effective_end_at or appointment.end_at,
            )
            resource_windows = resource_shift_cache.get(appointment.resource_id, shift_windows)
            if fits_one_shift(effective, tuple(resource_windows)):
                continue
            result.append(
                {
                    "appointment_id": str(appointment.id),
                    "time": local_start.strftime("%-I:%M %p"),
                    "patient": appointment.customer_name or "Patient",
                    "service": appointment.service_name_snapshot,
                    "phone": appointment.customer_phone,
                    "resource_name": appointment.resource_name_snapshot,
                }
            )
        return result

    # -----------------------------------------------------------------------
    # Non-destructive handlers (unchanged)
    # -----------------------------------------------------------------------

    async def _handle_get_summary(
        self, business_id: int, parsed: ParsedOwnerCommand
    ) -> OwnerCommandResult:
        target_date = await self._resolve_date(business_id, parsed.date)
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

        target_date = await self._resolve_date(business_id, parsed.for_date or parsed.valid_until)
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

    # -----------------------------------------------------------------------
    # Infrastructure helpers
    # -----------------------------------------------------------------------

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

    async def _lock_business_resources(self, business_id: int) -> None:
        await self._appointments.lock_business_schedule(business_id)
        resource_ids = await self._appointments.list_active_resource_ids(business_id)
        await self._appointments.lock_resource_schedules(business_id, resource_ids)

    async def _get_business_timezone(self, business_id: int) -> str:
        biz = await self._session.scalar(select(Business).where(Business.id == business_id))
        return biz.timezone if biz else "Asia/Kolkata"

    # -----------------------------------------------------------------------
    # Cancellation helpers
    # -----------------------------------------------------------------------

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
            await self._cancel_via_service(
                business_id, appt.id, appt.version, owner_phone, "owner_leave"
            )
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
            await self._cancel_via_service(
                business_id, appt.id, appt.version, owner_phone, "owner_closure"
            )
            cancelled.append(
                {
                    "time": appt.start_at.astimezone(tz).strftime("%-I:%M %p"),
                    "patient": appt.customer_name or "Patient",
                    "service": appt.service_name_snapshot,
                }
            )
        return cancelled

    async def _cancel_appointments_outside_schedule(
        self,
        business_id: int,
        target_date: date,
        owner_phone: str,
    ) -> list[dict[str, str]]:
        from fonely.services.availability import AvailabilityService

        timezone = await self._get_business_timezone(business_id)
        zone = ZoneInfo(timezone)
        svc = AvailabilityService(self._session)
        shift_windows = await svc._get_shift_windows(business_id, 0, target_date, timezone)
        all_resource_ids = await self._appointments.list_active_resource_ids(business_id)
        resource_shift_cache: dict[int, list[TimeWindow]] = {}
        for rid in all_resource_ids:
            resource_shift_cache[rid] = await svc._get_shift_windows(
                business_id, rid, target_date, timezone
            )

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
        for appointment in appointments:
            local_start = appointment.start_at.astimezone(zone)
            if local_start.date() != target_date:
                continue
            effective = TimeWindow(
                appointment.effective_start_at or appointment.start_at,
                appointment.effective_end_at or appointment.end_at,
            )
            resource_windows = resource_shift_cache.get(appointment.resource_id, shift_windows)
            if fits_one_shift(effective, tuple(resource_windows)):
                continue
            await self._cancel_via_service(
                business_id,
                appointment.id,
                appointment.version,
                owner_phone,
                "owner_close_early",
            )
            cancelled.append(
                {
                    "time": local_start.strftime("%-I:%M %p"),
                    "patient": appointment.customer_name or "Patient",
                    "service": appointment.service_name_snapshot,
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
    ) -> None:
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
        try:
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
        except AppointmentDomainError as exc:
            if exc.code in (
                AppointmentErrorCode.INVALID_STATE,
                AppointmentErrorCode.STALE_VERSION,
            ):
                reloaded = await self._appointments.get_by_business_and_id(
                    business_id, appointment_id
                )
                if reloaded is not None and reloaded.status == "cancelled":
                    return
            raise


# ---------------------------------------------------------------------------
# Standalone helper
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
