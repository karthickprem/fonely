"""Owner command execution service with durable two-phase confirmation."""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Literal, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
    NotificationOutboxEvent,
    OwnerAuditLog,
    OwnerCommandProposal,
    Resource,
    ScheduleException,
)
from fonely.repositories.appointments import AppointmentRepository
from fonely.repositories.owner_command_proposals import (
    OwnerCommandProposalRepository,
)
from fonely.services.model_gateway import ModelGateway
from fonely.services.owner_command_parser import (
    OwnerCommandParser,
    ParsedOwnerCommand,
)

_UNKNOWN_RESPONSE = (
    "Sorry, I didn't understand that command. You can say things like:\n"
    "- 'Dr. Priya leave tomorrow'\n"
    "- 'Close clinic early at 5'\n"
    "- 'Show tomorrow appointments'\n"
    "- 'This week consultation free'"
)

_DESTRUCTIVE_COMMANDS = frozenset({"doctor_leave", "close_clinic", "close_early"})
_CONFIRM_TOKENS = frozenset({"yes", "y", "confirm", "haan", "aamam", "ஆமாம்"})
_REJECT_TOKENS = frozenset({"no", "n", "cancel", "illai", "வேண்டாம்"})
_PROPOSAL_TTL = timedelta(minutes=5)


class OwnerCommandOutcomeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    operation: Literal["doctor_leave", "close_clinic", "close_early"]
    proposal_id: str = Field(min_length=1, max_length=36)
    proposal_version: int = Field(gt=0)
    business_id: int = Field(gt=0)
    payload_digest: str = Field(min_length=64, max_length=64)
    resolved_date: str
    clinic_timezone: str = Field(min_length=1, max_length=50)
    appointment_ids: list[int]
    affected_appointments: int = Field(ge=0)
    affected_patients: int = Field(ge=0)
    schedule_exception: dict[str, object]
    queued_outbox_ids: list[int]
    queued_outbox_count: int = Field(ge=0)
    audit_id: int = Field(gt=0)
    status: Literal["completed"] = "completed"
    completed_at: str
    presentation_text: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_counts(self) -> "OwnerCommandOutcomeEvidence":
        if self.affected_appointments != len(self.appointment_ids):
            raise ValueError("affected appointment count does not match IDs")
        if self.affected_patients != self.affected_appointments:
            raise ValueError("affected patient count does not match appointments")
        if self.queued_outbox_count != len(self.queued_outbox_ids):
            raise ValueError("queued outbox count does not match IDs")
        return self


@dataclass(frozen=True)
class OwnerCommandResult:
    command_type: str
    success: bool
    response_text: str
    affected_appointments: int = 0
    affected_patients: int = 0
    details: list[str] = field(default_factory=list)
    proposal_id: str | None = None


class OwnerCommandService:
    def __init__(self, session: AsyncSession, model: ModelGateway) -> None:
        self._session = session
        self._parser = OwnerCommandParser(model)
        self._appointments = AppointmentRepository(session)
        self._proposals = OwnerCommandProposalRepository(session)

    async def process_command(
        self,
        business_id: int,
        owner_phone: str,
        message: str,
        *,
        owner_user_id: int | None = None,
    ) -> OwnerCommandResult:
        owner = await self._require_active_owner(business_id, owner_phone)
        uid = owner_user_id or owner.id

        token = message.strip().lower()
        if token in _CONFIRM_TOKENS:
            proposal = await self._proposals.get_latest_for_owner(
                business_id,
                uid,
                owner_phone,
                statuses=("pending_confirmation", "completed"),
                for_update=False,
            )
            if proposal is not None:
                if proposal.status == "completed":
                    return self._result_from_completed_proposal(proposal)
                if proposal.expires_at <= datetime.now(UTC):
                    await self._proposals.transition_status(
                        business_id,
                        proposal.id,
                        proposal.expected_version,
                        "pending_confirmation",
                        "expired",
                    )
                    return OwnerCommandResult(
                        command_type=proposal.command_type,
                        success=False,
                        response_text="That command expired. Please send it again.",
                    )
                return await self._confirm_proposal(business_id, owner, proposal)

        pending = await self._proposals.get_latest_pending_for_owner(
            business_id, uid, owner_phone, for_update=False
        )
        if pending is not None:
            if pending.expires_at <= datetime.now(UTC):
                await self._proposals.transition_status(
                    business_id,
                    pending.id,
                    pending.expected_version,
                    "pending_confirmation",
                    "expired",
                )
            elif token in _REJECT_TOKENS:
                await self._proposals.transition_status(
                    business_id,
                    pending.id,
                    pending.expected_version,
                    "pending_confirmation",
                    "rejected",
                )
                return OwnerCommandResult(
                    command_type=pending.command_type,
                    success=True,
                    response_text="Cancelled. No changes made.",
                )
            else:
                return OwnerCommandResult(
                    command_type=pending.command_type,
                    success=False,
                    response_text=(
                        "You have a pending command. Reply YES to confirm or NO to cancel."
                    ),
                    proposal_id=pending.id,
                )

        doctor_names = await self._get_doctor_names(business_id)
        parsed = await self._parser.parse(message, doctor_names)

        if parsed.command in _DESTRUCTIVE_COMMANDS:
            return await self._create_preview(business_id, owner, parsed)

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

        return OwnerCommandResult(
            command_type="unknown",
            success=False,
            response_text=_UNKNOWN_RESPONSE,
        )

    async def _require_active_owner(self, business_id: int, owner_phone: str) -> BusinessUser:
        user = (
            await self._session.execute(
                select(BusinessUser).where(
                    BusinessUser.business_id == business_id,
                    BusinessUser.phone == owner_phone,
                    BusinessUser.role == "owner",
                    BusinessUser.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if user is None:
            raise PermissionError("owner_not_found_or_inactive")
        return user

    async def _create_preview(
        self,
        business_id: int,
        owner: BusinessUser,
        parsed: ParsedOwnerCommand,
    ) -> OwnerCommandResult:
        target_date = await self._resolve_date(business_id, parsed.date)
        tz_name = await self._get_business_timezone(business_id)
        tz = ZoneInfo(tz_name)

        if parsed.command == "doctor_leave":
            resource = await self._resolve_resource(business_id, parsed.doctor_name)
            if resource is None:
                names = await self._get_doctor_names(business_id)
                return OwnerCommandResult(
                    command_type="doctor_leave",
                    success=False,
                    response_text=(
                        f"Could not find doctor '{parsed.doctor_name}'. "
                        f"Available: {', '.join(names)}"
                    ),
                )
            await self._appointments.lock_resource_schedule(business_id, resource.id)
            targets = await self._appointments_for_resource(
                business_id, resource.id, target_date, tz
            )
            snapshot = self._build_preview_snapshot(
                command_type="doctor_leave",
                resolved_date=target_date,
                clinic_timezone=tz_name,
                resource_id=resource.id,
                resource_name=resource.name,
                schedule_mutation={
                    "type": "schedule_exception",
                    "is_closed": True,
                    "reason": parsed.reason or "Leave",
                },
                close_time=None,
                appointments=targets,
            )
        elif parsed.command == "close_clinic":
            await self._lock_business_resources(business_id)
            targets = await self._all_appointments_on_date(business_id, target_date, tz)
            snapshot = self._build_preview_snapshot(
                command_type="close_clinic",
                resolved_date=target_date,
                clinic_timezone=tz_name,
                resource_id=None,
                resource_name=None,
                schedule_mutation={
                    "type": "schedule_exception",
                    "is_closed": True,
                    "reason": parsed.reason or "Closed",
                },
                close_time=None,
                appointments=targets,
            )
        elif parsed.command == "close_early":
            result = await self._validate_close_early(business_id, parsed, target_date, tz_name)
            if isinstance(result, OwnerCommandResult):
                return result
            new_close, truncated, affected = result
            snapshot = self._build_preview_snapshot(
                command_type="close_early",
                resolved_date=target_date,
                clinic_timezone=tz_name,
                resource_id=None,
                resource_name=None,
                schedule_mutation={
                    "type": "schedule_exception",
                    "is_closed": len(truncated) == 0,
                    "open_time": (truncated[0].open_time.isoformat() if truncated else None),
                    "close_time": (truncated[0].close_time.isoformat() if truncated else None),
                    "reason": parsed.reason or "Closing early",
                },
                close_time=new_close.isoformat(),
                appointments=affected,
            )
        else:
            return OwnerCommandResult(
                command_type=parsed.command,
                success=False,
                response_text=_UNKNOWN_RESPONSE,
            )

        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str).encode()
        ).hexdigest()
        idem_key = (
            f"owner-{parsed.command}-{business_id}-"
            f"{snapshot.get('resource_id', 'all')}-{target_date.isoformat()}"
        )

        proposal = await self._proposals.create_idempotent(
            {
                "id": uuid.uuid4().hex[:36],
                "business_id": business_id,
                "owner_user_id": owner.id,
                "owner_phone_snapshot": owner.phone,
                "command_type": parsed.command,
                "command_payload": {
                    "date": target_date.isoformat(),
                    "doctor_name": parsed.doctor_name,
                    "close_time": parsed.close_time,
                    "reason": parsed.reason,
                },
                "preview_snapshot": snapshot,
                "payload_digest": digest,
                "idempotency_key": idem_key,
                "expires_at": datetime.now(UTC) + _PROPOSAL_TTL,
            }
        )
        if proposal is None:
            existing = await self._proposals.get_by_idempotency_key(business_id, idem_key)
            if existing and existing.status == "completed":
                return OwnerCommandResult(
                    command_type=parsed.command,
                    success=True,
                    response_text="This command was already completed.",
                    proposal_id=existing.id,
                )
            if existing and existing.status == "pending_confirmation":
                proposal = existing
            else:
                return OwnerCommandResult(
                    command_type=parsed.command,
                    success=False,
                    response_text=("A conflicting command exists. Please wait and try again."),
                )

        raw_appointments = snapshot.get("appointments", [])
        appt_list = raw_appointments if isinstance(raw_appointments, list) else []
        date_str = target_date.strftime("%b %d")
        if parsed.command == "doctor_leave":
            rname = snapshot.get("resource_name", "Doctor")
            lines = [f"Mark {rname} on leave for {date_str}?"]
        elif parsed.command == "close_clinic":
            lines = [f"Close clinic on {date_str}?"]
        else:
            raw_mutation = snapshot.get("schedule_mutation", {})
            mutation = raw_mutation if isinstance(raw_mutation, dict) else {}
            ct = mutation.get("close_time", "")
            lines = [f"Close clinic early at {ct} on {date_str}?"]

        if appt_list:
            lines.append(f"This will cancel {len(appt_list)} appointment(s):")
            for item in appt_list:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"  - {item['start_at_local']} "
                    f"{item.get('patient_display', 'Patient')} "
                    f"({item.get('service_name', '')})"
                )
        else:
            lines.append("No appointments will be affected.")
        lines.append("\nReply YES to confirm or NO to cancel.")

        return OwnerCommandResult(
            command_type=parsed.command,
            success=True,
            response_text="\n".join(lines),
            affected_appointments=len(appt_list),
            proposal_id=proposal.id,
        )

    @staticmethod
    def _result_from_completed_proposal(
        proposal: OwnerCommandProposal,
    ) -> OwnerCommandResult:
        try:
            evidence = OwnerCommandOutcomeEvidence.model_validate(proposal.result_evidence)
        except Exception as exc:
            raise RuntimeError("completed_owner_proposal_evidence_invalid") from exc
        if (
            evidence.status != "completed"
            or evidence.operation != proposal.command_type
            or evidence.proposal_id != proposal.id
            or evidence.business_id != proposal.business_id
            or evidence.payload_digest != proposal.payload_digest
        ):
            raise RuntimeError("completed_owner_proposal_evidence_mismatch")
        response_text = evidence.presentation_text or (
            f"Done. {evidence.affected_appointments} appointment(s) cancelled "
            f"on {evidence.resolved_date}. Notifications queued."
            if evidence.affected_appointments
            else (f"Done. Schedule updated for {evidence.resolved_date}. No appointments affected.")
        )
        return OwnerCommandResult(
            command_type=evidence.operation,
            success=True,
            response_text=response_text,
            affected_appointments=evidence.affected_appointments,
            affected_patients=evidence.affected_patients,
            proposal_id=evidence.proposal_id,
        )

    async def _confirm_proposal(
        self,
        business_id: int,
        owner: BusinessUser,
        proposal: OwnerCommandProposal,
    ) -> OwnerCommandResult:
        if proposal.status == "completed":
            return self._result_from_completed_proposal(proposal)
        if (
            proposal.business_id != business_id
            or proposal.owner_user_id != owner.id
            or proposal.owner_phone_snapshot != owner.phone
        ):
            raise PermissionError("owner_proposal_actor_mismatch")

        snapshot = proposal.preview_snapshot
        if not isinstance(snapshot, dict):
            raise RuntimeError("owner_proposal_snapshot_invalid")
        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str).encode()
        ).hexdigest()
        if digest != proposal.payload_digest:
            return await self._fail_proposal(
                proposal,
                "payload_digest_mismatch",
                "Stored preview evidence changed",
            )

        raw_targets = snapshot.get("appointments", [])
        if not isinstance(raw_targets, list) or not all(
            isinstance(target, dict) for target in raw_targets
        ):
            raise RuntimeError("owner_proposal_targets_invalid")
        targets: list[dict[str, object]] = raw_targets
        raw_mutation = snapshot.get("schedule_mutation", {})
        if not isinstance(raw_mutation, dict):
            raise RuntimeError("owner_proposal_mutation_invalid")
        mutation: dict[str, object] = raw_mutation
        resource_id_raw = snapshot.get("resource_id")
        resource_id = int(str(resource_id_raw)) if resource_id_raw is not None else None

        if resource_id is None:
            await self._lock_business_resources(business_id)
        else:
            await self._appointments.lock_resource_schedule(business_id, resource_id)

        locked_appointments: list[Appointment] = []
        for target in sorted(targets, key=lambda item: int(str(item["appointment_id"]))):
            appointment_id = int(str(target["appointment_id"]))
            appt = await self._appointments.lock_appointment(business_id, appointment_id)
            if appt is None:
                return await self._fail_proposal(
                    proposal,
                    "target_not_found",
                    f"Appointment {appointment_id} not found",
                )
            if (
                appt.status != "confirmed"
                or appt.version != int(str(target["expected_version"]))
                or appt.resource_id != int(str(target["resource_id"]))
                or appt.start_at.isoformat() != str(target["start_at_utc"])
            ):
                return await self._fail_proposal(
                    proposal,
                    "target_drift",
                    f"Appointment {appointment_id} changed after preview",
                )
            locked_appointments.append(appt)

        locked_proposal = await self._proposals.get_by_id(business_id, proposal.id, for_update=True)
        if locked_proposal is None:
            raise RuntimeError("owner_proposal_not_found")
        if locked_proposal.status == "completed":
            return self._result_from_completed_proposal(locked_proposal)
        if (
            locked_proposal.status != "pending_confirmation"
            or locked_proposal.expected_version != proposal.expected_version
            or locked_proposal.owner_user_id != owner.id
            or locked_proposal.owner_phone_snapshot != owner.phone
            or locked_proposal.payload_digest != digest
        ):
            return OwnerCommandResult(
                command_type=proposal.command_type,
                success=False,
                response_text="Command state changed. Please send the command again.",
            )

        result: OwnerCommandResult
        async with self._session.begin_nested():
            transitioned = await self._proposals.transition_status(
                business_id,
                locked_proposal.id,
                locked_proposal.expected_version,
                "pending_confirmation",
                "executing",
                confirmed_at=datetime.now(UTC),
            )
            if transitioned is None:
                raise RuntimeError("owner_proposal_transition_conflict")

            schedule_exception = await self._ensure_schedule_exception(
                business_id,
                resource_id,
                date.fromisoformat(str(snapshot["resolved_date"])),
                mutation,
            )

            for target, appt in zip(
                sorted(targets, key=lambda item: int(str(item["appointment_id"]))),
                locked_appointments,
                strict=True,
            ):
                await self._cancel_via_service(
                    business_id,
                    appt.id,
                    appt.version,
                    owner.phone,
                    str(target.get("expected_cancellation_reason", "owner_command")),
                    proposal_id=proposal.id,
                )

            cancelled_count = len(locked_appointments)
            date_str = str(snapshot.get("resolved_date", ""))
            if cancelled_count:
                response_text = (
                    f"Done. {cancelled_count} appointment(s) cancelled "
                    f"on {date_str}. Notifications queued."
                )
            else:
                response_text = f"Done. Schedule updated for {date_str}. No appointments affected."
            audit = OwnerAuditLog(
                business_id=business_id,
                initiated_by_phone=owner.phone,
                action=proposal.command_type,
                details={
                    "proposal_id": proposal.id,
                    "targets_count": len(targets),
                    "cancelled_count": cancelled_count,
                    "resolved_date": date_str,
                    "payload_digest": digest,
                },
            )
            self._session.add(audit)
            await self._session.flush()

            outbox_ids = list(
                (
                    await self._session.scalars(
                        select(NotificationOutboxEvent.id).where(
                            NotificationOutboxEvent.business_id == business_id,
                            NotificationOutboxEvent.entity_type == "appointment",
                            NotificationOutboxEvent.entity_id.in_(
                                [appt.id for appt in locked_appointments]
                            ),
                            NotificationOutboxEvent.event_type == "appointment_cancelled",
                        )
                    )
                ).all()
            )
            completed_at = datetime.now(UTC)
            outcome = OwnerCommandOutcomeEvidence(
                schema_version=1,
                operation=cast(
                    Literal["doctor_leave", "close_clinic", "close_early"],
                    proposal.command_type,
                ),
                proposal_id=proposal.id,
                proposal_version=transitioned.expected_version + 1,
                business_id=business_id,
                payload_digest=digest,
                resolved_date=date_str,
                clinic_timezone=str(snapshot["clinic_timezone"]),
                appointment_ids=[appt.id for appt in locked_appointments],
                affected_appointments=cancelled_count,
                affected_patients=cancelled_count,
                schedule_exception={
                    "id": schedule_exception.id,
                    "resource_id": schedule_exception.resource_id,
                    "exception_date": schedule_exception.exception_date.isoformat(),
                    "is_closed": schedule_exception.is_closed,
                    "open_time": (
                        schedule_exception.open_time.isoformat()
                        if schedule_exception.open_time
                        else None
                    ),
                    "close_time": (
                        schedule_exception.close_time.isoformat()
                        if schedule_exception.close_time
                        else None
                    ),
                    "reason": schedule_exception.reason,
                },
                queued_outbox_ids=outbox_ids,
                queued_outbox_count=len(outbox_ids),
                audit_id=audit.id,
                completed_at=completed_at.isoformat(),
                presentation_text=response_text,
            )
            result_evidence = outcome.model_dump(mode="json")

            completed = await self._proposals.transition_status(
                business_id,
                proposal.id,
                transitioned.expected_version,
                "executing",
                "completed",
                completed_at=completed_at,
                result_evidence=result_evidence,
            )
            if completed is None:
                raise RuntimeError("owner_proposal_completion_conflict")
            result = self._result_from_completed_proposal(completed)

        return result

    async def _fail_proposal(
        self,
        proposal: OwnerCommandProposal,
        failure_code: str,
        failure_message: str,
    ) -> OwnerCommandResult:
        failed = await self._proposals.transition_status(
            proposal.business_id,
            proposal.id,
            proposal.expected_version,
            "pending_confirmation",
            "failed",
            failure_code=failure_code,
            failure_message=failure_message[:500],
        )
        if failed is None:
            current = await self._proposals.get_by_id(
                proposal.business_id, proposal.id, for_update=True
            )
            if current is not None and current.status == "completed":
                return self._result_from_completed_proposal(current)
            return OwnerCommandResult(
                command_type=proposal.command_type,
                success=False,
                response_text="Command state changed. Please send the command again.",
                proposal_id=proposal.id,
            )
        return OwnerCommandResult(
            command_type=proposal.command_type,
            success=False,
            response_text="Schedule changed since preview. Please send the command again.",
            proposal_id=proposal.id,
        )

    async def _ensure_schedule_exception(
        self,
        business_id: int,
        resource_id: int | None,
        exception_date: date,
        mutation: dict[str, object],
    ) -> ScheduleException:
        conditions = [
            ScheduleException.business_id == business_id,
            ScheduleException.exception_date == exception_date,
        ]
        if resource_id is None:
            conditions.append(ScheduleException.resource_id.is_(None))
        else:
            conditions.append(ScheduleException.resource_id == resource_id)
        existing = (
            await self._session.execute(select(ScheduleException).where(*conditions))
        ).scalar_one_or_none()

        is_closed = bool(mutation.get("is_closed"))
        open_time = (
            dt_time.fromisoformat(str(mutation["open_time"])) if mutation.get("open_time") else None
        )
        close_time = (
            dt_time.fromisoformat(str(mutation["close_time"]))
            if mutation.get("close_time")
            else None
        )
        reason = str(mutation.get("reason", "Owner command"))
        if existing is not None:
            if (
                existing.is_closed == is_closed
                and existing.open_time == open_time
                and existing.close_time == close_time
                and existing.reason == reason
            ):
                return existing
            raise RuntimeError("schedule_exception_drift")

        exception = ScheduleException(
            business_id=business_id,
            resource_id=resource_id,
            exception_date=exception_date,
            is_closed=is_closed,
            open_time=open_time,
            close_time=close_time,
            reason=reason,
        )
        self._session.add(exception)
        await self._session.flush()
        return exception

    @staticmethod
    def _build_preview_snapshot(
        *,
        command_type: str,
        resolved_date: date,
        clinic_timezone: str,
        resource_id: int | None,
        resource_name: str | None,
        schedule_mutation: dict[str, object],
        close_time: str | None,
        appointments: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "command_type": command_type,
            "resolved_date": resolved_date.isoformat(),
            "clinic_timezone": clinic_timezone,
            "resource_id": resource_id,
            "resource_name": resource_name,
            "schedule_mutation": schedule_mutation,
            "close_time": close_time,
            "appointments": appointments,
        }

    async def _appointments_for_resource(
        self,
        business_id: int,
        resource_id: int,
        target_date: date,
        tz: ZoneInfo,
    ) -> list[dict[str, object]]:
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
        result: list[dict[str, object]] = []
        for appt in sorted(appointments, key=lambda a: a.id):
            if appt.start_at.astimezone(tz).date() != target_date:
                continue
            result.append(self._appointment_target(appt, tz, "owner_leave"))
        return result

    async def _all_appointments_on_date(
        self,
        business_id: int,
        target_date: date,
        tz: ZoneInfo,
    ) -> list[dict[str, object]]:
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
        result: list[dict[str, object]] = []
        for appt in sorted(appointments, key=lambda a: a.id):
            if appt.start_at.astimezone(tz).date() != target_date:
                continue
            result.append(self._appointment_target(appt, tz, "owner_closure"))
        return result

    @staticmethod
    def _appointment_target(
        appt: Appointment,
        tz: ZoneInfo,
        reason: str,
    ) -> dict[str, object]:
        local_start = appt.start_at.astimezone(tz)
        return {
            "appointment_id": appt.id,
            "expected_version": appt.version,
            "start_at_utc": appt.start_at.isoformat(),
            "start_at_local": local_start.strftime("%-I:%M %p"),
            "service_id": appt.service_id,
            "service_name": appt.service_name_snapshot,
            "resource_id": appt.resource_id,
            "resource_name": appt.resource_name_snapshot,
            "patient_display": appt.customer_name or "Patient",
            "status": appt.status,
            "expected_cancellation_reason": reason,
        }

    async def _validate_close_early(
        self,
        business_id: int,
        parsed: ParsedOwnerCommand,
        target_date: date,
        tz_name: str,
    ) -> OwnerCommandResult | tuple[dt_time, list[LocalShift], list[dict[str, object]]]:
        from fonely.models.schema import OperatingSchedule

        target_date = await self._resolve_date(business_id, parsed.date)

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

        affected = await self._appointments_outside_schedule(
            business_id, target_date, tz_name, truncated
        )
        return new_close, list(truncated), affected

    async def _appointments_outside_schedule(
        self,
        business_id: int,
        target_date: date,
        tz_name: str,
        allowed_shifts: tuple[LocalShift, ...] | list[LocalShift],
    ) -> list[dict[str, object]]:
        from fonely.services.availability import AvailabilityService

        zone = ZoneInfo(tz_name)
        svc = AvailabilityService(self._session)
        shift_windows = [
            TimeWindow(
                datetime.combine(target_date, s.open_time, zone),
                datetime.combine(target_date, s.close_time, zone),
            )
            for s in allowed_shifts
        ]
        all_resource_ids = await self._appointments.list_active_resource_ids(business_id)
        resource_shift_cache: dict[int, list[TimeWindow]] = {}
        for rid in all_resource_ids:
            resource_shift_cache[rid] = await svc._get_shift_windows(
                business_id, rid, target_date, tz_name
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

        result: list[dict[str, object]] = []
        for appt in sorted(appointments, key=lambda a: a.id):
            local_start = appt.start_at.astimezone(zone)
            if local_start.date() != target_date:
                continue
            effective = TimeWindow(
                appt.effective_start_at or appt.start_at,
                appt.effective_end_at or appt.end_at,
            )
            resource_windows = resource_shift_cache.get(appt.resource_id, shift_windows)
            if fits_one_shift(effective, tuple(resource_windows)):
                continue
            result.append(self._appointment_target(appt, zone, "owner_close_early"))
        return result

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

    async def _lock_business_resources(self, business_id: int) -> None:
        await self._appointments.lock_business_schedule(business_id)
        resource_ids = await self._appointments.list_active_resource_ids(business_id)
        await self._appointments.lock_resource_schedules(business_id, resource_ids)

    async def _resolve_date(self, business_id: int, expr: str | None) -> date:
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

    async def _get_business_timezone(self, business_id: int) -> str:
        biz = await self._session.scalar(select(Business).where(Business.id == business_id))
        return biz.timezone if biz else "Asia/Kolkata"

    async def _cancel_via_service(
        self,
        business_id: int,
        appointment_id: int,
        appointment_version: int,
        owner_phone: str,
        reason_code: str,
        *,
        proposal_id: str,
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
        key = f"owner-{proposal_id}-cancel-{appointment_id}"
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
