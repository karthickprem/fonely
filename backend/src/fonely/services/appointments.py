"""D3 appointment create, cancel, and reschedule application transactions."""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCancellationCommand,
    ConfirmPendingAppointmentCommand,
    ConfirmPendingAppointmentRescheduleCommand,
    CreatePendingAppointmentCancellationCommand,
    CreatePendingAppointmentCommand,
    CreatePendingAppointmentRescheduleCommand,
)
from fonely.domain.appointments.commit_contract import (
    APPOINTMENT_CANCEL_POST_COMPLETION_CONSTRAINTS,
    APPOINTMENT_CANCEL_PRE_COMPLETION_CONSTRAINTS,
    APPOINTMENT_CREATE_POST_COMPLETION_CONSTRAINTS,
    APPOINTMENT_CREATE_PRE_COMPLETION_CONSTRAINTS,
    APPOINTMENT_RESCHEDULE_POST_COMPLETION_CONSTRAINTS,
    APPOINTMENT_RESCHEDULE_PRE_COMPLETION_CONSTRAINTS,
    set_constraints_immediate_sql,
)
from fonely.domain.appointments.errors import AppointmentDomainError, AppointmentErrorCode
from fonely.domain.appointments.results import (
    AppointmentCancellationResult,
    AppointmentCommitFailureCode,
    AppointmentConfirmationResult,
    AppointmentProposalResult,
    AppointmentRescheduleResult,
    ConfirmationFactsResult,
    ConfirmationSchedulingFacts,
    PreCommitAppointmentFailure,
    PreCommitAppointmentOutcome,
    PreCommitAppointmentSuccess,
)
from fonely.domain.appointments.validation import AppointmentValidationPort
from fonely.domain.pending_actions.commands import (
    BeginCommitCommand,
    CommitResultContext,
    CompleteCommitCommand,
    CreatePendingActionCommand,
    FailCommitCommand,
    MarkAwaitingConfirmationCommand,
)
from fonely.domain.pending_actions.payloads import (
    AppointmentFacts,
    CancelAppointmentData,
    CreateAppointmentData,
    PendingAppointmentEnvelope,
    RescheduleAppointmentData,
)
from fonely.domain.pending_actions.snapshots import canonical_payload_dict
from fonely.models.enums import (
    AppointmentSource,
    AppointmentStatus,
    PendingActionType,
    ResourceAllocationSource,
    ResourceAllocationStatus,
    ResourceAllocationType,
)
from fonely.repositories.appointments import AppointmentRepository
from fonely.services.authorization import require_existing_action_permission
from fonely.services.pending_actions import PendingActionService

logger = logging.getLogger("fonely.services.appointments")

_OVERLAP_CONSTRAINT = "ex_resource_allocations_active_overlap"
_OVERLAP_SQLSTATE = "23P01"

_DEFERRED_RESTORE_SQL = "SET CONSTRAINTS {} DEFERRED"


def _pg_constraint_name(error: IntegrityError) -> str | None:
    cause: BaseException | None = error.orig
    while cause is not None:
        name: str | None = getattr(cause, "constraint_name", None)
        if name is not None:
            return name
        cause = getattr(cause, "__cause__", None)
    return None


def _restore_deferred_sql(constraint_names: tuple[str, ...]) -> str:
    return _DEFERRED_RESTORE_SQL.format(", ".join(constraint_names))


class AppointmentService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        validation: AppointmentValidationPort,
    ) -> None:
        self._session = session
        self._validation = validation
        self._repo = AppointmentRepository(session)
        self._pa_service = PendingActionService(
            session,
            appointment_validation=validation,
        )

    async def create_proposal(
        self,
        command: CreatePendingAppointmentCommand,
    ) -> AppointmentProposalResult:
        resolved_envelope = await self._resolve_envelope(command)
        assert isinstance(resolved_envelope.data, CreateAppointmentData)

        pa_result = await self._pa_service.create(
            CreatePendingActionCommand(
                actor=command.actor,
                action_type=PendingActionType.APPOINTMENT,
                payload=canonical_payload_dict(resolved_envelope),
                expires_at=command.expires_at,
                idempotency_key=command.idempotency_key,
            )
        )

        if pa_result.status == "collecting_details":
            pa_result = await self._pa_service.mark_awaiting_confirmation(
                MarkAwaitingConfirmationCommand(
                    actor=command.actor,
                    action_id=pa_result.id,
                    expected_version=pa_result.version,
                )
            )

        stored_envelope = PendingAppointmentEnvelope.model_validate(pa_result.payload)
        assert isinstance(stored_envelope.data, CreateAppointmentData)
        stored_facts = stored_envelope.data.facts

        return AppointmentProposalResult(
            pending_action_id=pa_result.id,
            version=pa_result.version,
            expires_at=pa_result.expires_at,
            confirmation_facts=self._to_confirmation_facts(stored_facts),
        )

    async def confirm_and_commit(
        self,
        command: ConfirmPendingAppointmentCommand,
    ) -> PreCommitAppointmentOutcome:
        action = await self._pa_service._require_action(
            command.actor.business_id,
            command.pending_action_id,
        )
        await require_existing_action_permission(self._session, command.actor, action)

        existing = await self._repo.get_by_business_and_pending_action(
            command.actor.business_id,
            command.pending_action_id,
        )
        if existing is not None:
            return self._replay_result(existing, action.version)

        context = CommitResultContext(
            business_id=command.actor.business_id,
            pending_action_id=command.pending_action_id,
            expected_version=command.expected_version,
            engine="appointment_engine",
        )

        begin_result = await self._pa_service.begin_commit(BeginCommitCommand(context=context))
        committing_version = begin_result.version
        committing_context = CommitResultContext(
            business_id=context.business_id,
            pending_action_id=context.pending_action_id,
            expected_version=committing_version,
            engine="appointment_engine",
        )

        envelope = PendingAppointmentEnvelope.model_validate(begin_result.payload)
        data = envelope.data
        assert isinstance(data, CreateAppointmentData)
        facts = data.facts

        await self._repo.lock_resource_schedule(
            command.actor.business_id,
            facts.resource_id,
        )

        overlap_exc: IntegrityError | None = None
        try:
            async with self._session.begin_nested():
                appointment = await self._repo.insert(
                    {
                        "business_id": command.actor.business_id,
                        "resource_id": facts.resource_id,
                        "service_id": facts.service_id,
                        "customer_name": data.customer_name,
                        "customer_phone": data.customer_phone,
                        "start_at": facts.start_at,
                        "end_at": facts.end_at,
                        "effective_start_at": facts.effective_start_at,
                        "effective_end_at": facts.effective_end_at,
                        "service_name_snapshot": facts.service_name,
                        "resource_name_snapshot": facts.resource_name,
                        "duration_minutes_snapshot": facts.duration_minutes,
                        "buffer_before_minutes_snapshot": facts.buffer_before_minutes,
                        "buffer_after_minutes_snapshot": facts.buffer_after_minutes,
                        "price_snapshot": (
                            Decimal(str(facts.price)) if facts.price is not None else None
                        ),
                        "business_timezone_snapshot": facts.business_timezone,
                        "reason": data.reason,
                        "status": AppointmentStatus.CONFIRMED.value,
                        "source": AppointmentSource.CUSTOMER_CONVERSATION.value,
                        "idempotency_key": f"pa-{context.pending_action_id}",
                        "pending_action_id": context.pending_action_id,
                        "call_id": data.call_id,
                    }
                )

                await self._repo.insert_allocation(
                    {
                        "business_id": command.actor.business_id,
                        "resource_id": facts.resource_id,
                        "appointment_id": appointment.id,
                        "pending_action_id": context.pending_action_id,
                        "allocation_type": ResourceAllocationType.APPOINTMENT.value,
                        "status": ResourceAllocationStatus.ACTIVE.value,
                        "source": ResourceAllocationSource.CUSTOMER_CONVERSATION.value,
                        "effective_start_at": facts.effective_start_at,
                        "effective_end_at": facts.effective_end_at,
                        "idempotency_key": f"appt-{appointment.id}",
                    }
                )

                await self._repo.force_constraints(
                    set_constraints_immediate_sql(APPOINTMENT_CREATE_PRE_COMPLETION_CONSTRAINTS)
                )
                await self._repo.force_constraints(
                    _restore_deferred_sql(APPOINTMENT_CREATE_PRE_COMPLETION_CONSTRAINTS)
                )

                complete_result = await self._pa_service.complete_commit(
                    CompleteCommitCommand(
                        context=committing_context,
                        committed_entity_type="appointment",
                        committed_entity_id=appointment.id,
                    )
                )

                await self._repo.force_constraints(
                    set_constraints_immediate_sql(APPOINTMENT_CREATE_POST_COMPLETION_CONSTRAINTS)
                )
                await self._repo.force_constraints(
                    _restore_deferred_sql(APPOINTMENT_CREATE_POST_COMPLETION_CONSTRAINTS)
                )

                try:
                    from fonely.services.notifications import NotificationService

                    await NotificationService(self._session).create_appointment_notifications(
                        business_id=command.actor.business_id,
                        appointment_id=appointment.id,
                        customer_phone=data.customer_phone,
                        customer_name=data.customer_name,
                        service_name=facts.service_name,
                        resource_name=facts.resource_name,
                        start_at=facts.start_at,
                        price=facts.price,
                        business_timezone=facts.business_timezone,
                    )
                except Exception:
                    logger.warning("notification_outbox_insert_failed", exc_info=True)
        except IntegrityError as exc:
            if (
                getattr(exc.orig, "sqlstate", None) == _OVERLAP_SQLSTATE
                and _pg_constraint_name(exc) == _OVERLAP_CONSTRAINT
            ):
                overlap_exc = exc
            else:
                raise

        if overlap_exc is not None:
            fail_result = await self._pa_service.fail_commit(
                FailCommitCommand(
                    context=committing_context,
                    error_code="resource_unavailable",
                    retryable=True,
                )
            )
            return PreCommitAppointmentFailure(
                pending_action_id=context.pending_action_id,
                pending_action_version=fail_result.version,
                error_code=AppointmentCommitFailureCode.RESOURCE_UNAVAILABLE,
            )

        return PreCommitAppointmentSuccess(
            appointment=AppointmentConfirmationResult(
                appointment_id=appointment.id,
                pending_action_id=context.pending_action_id,
                service_id=facts.service_id,
                service_name=facts.service_name,
                resource_id=facts.resource_id,
                resource_name=facts.resource_name,
                start_at=facts.start_at,
                end_at=facts.end_at,
                price=facts.price,
                business_timezone=facts.business_timezone,
            ),
            pending_action_version=complete_result.version,
        )

    async def create_cancellation_proposal(
        self,
        command: CreatePendingAppointmentCancellationCommand,
    ) -> AppointmentProposalResult:
        appointment = await self._repo.get_by_business_and_id(
            command.actor.business_id,
            command.appointment_id,
        )
        if appointment is None:
            raise AppointmentDomainError(AppointmentErrorCode.NOT_FOUND, "Appointment not found")
        if appointment.status != AppointmentStatus.CONFIRMED:
            raise AppointmentDomainError(
                AppointmentErrorCode.INVALID_STATE,
                f"Cannot cancel appointment in status {appointment.status}",
            )

        current_facts = self._appointment_to_facts(appointment)
        envelope = PendingAppointmentEnvelope(
            data=CancelAppointmentData(
                target_appointment_id=command.appointment_id,
                target_expected_version=command.expected_appointment_version,
                current_facts=current_facts,
                reason_code=command.reason_code,
            )
        )

        pa_result = await self._pa_service.create(
            CreatePendingActionCommand(
                actor=command.actor,
                action_type=PendingActionType.APPOINTMENT,
                payload=canonical_payload_dict(envelope),
                expires_at=command.expires_at,
                idempotency_key=command.idempotency_key,
            )
        )

        if pa_result.status == "collecting_details":
            pa_result = await self._pa_service.mark_awaiting_confirmation(
                MarkAwaitingConfirmationCommand(
                    actor=command.actor,
                    action_id=pa_result.id,
                    expected_version=pa_result.version,
                )
            )

        return AppointmentProposalResult(
            pending_action_id=pa_result.id,
            version=pa_result.version,
            expires_at=pa_result.expires_at,
            confirmation_facts=ConfirmationFactsResult(
                operation="cancel",
                service_id=current_facts.service_id,
                service_name=current_facts.service_name,
                resource_id=current_facts.resource_id,
                resource_name=current_facts.resource_name,
                start_at=current_facts.start_at.isoformat(),
                end_at=current_facts.end_at.isoformat(),
                duration_minutes=current_facts.duration_minutes,
                price=str(current_facts.price) if current_facts.price is not None else None,
                business_timezone=current_facts.business_timezone,
                target_appointment_id=command.appointment_id,
                reason_code=command.reason_code,
            ),
        )

    async def confirm_cancellation(
        self,
        command: ConfirmPendingAppointmentCancellationCommand,
    ) -> AppointmentCancellationResult:
        action = await self._pa_service._require_action(
            command.actor.business_id,
            command.pending_action_id,
        )
        await require_existing_action_permission(self._session, command.actor, action)

        envelope = PendingAppointmentEnvelope.model_validate(action.proposed_payload)
        data = envelope.data
        assert isinstance(data, CancelAppointmentData)

        context = CommitResultContext(
            business_id=command.actor.business_id,
            pending_action_id=command.pending_action_id,
            expected_version=command.expected_version,
            engine="appointment_engine",
        )

        begin_result = await self._pa_service.begin_commit(BeginCommitCommand(context=context))
        committing_context = CommitResultContext(
            business_id=context.business_id,
            pending_action_id=context.pending_action_id,
            expected_version=begin_result.version,
            engine="appointment_engine",
        )

        appointment = await self._repo.lock_appointment(
            command.actor.business_id,
            data.target_appointment_id,
        )
        if appointment is None:
            raise AppointmentDomainError(
                AppointmentErrorCode.NOT_FOUND, "Target appointment not found"
            )
        if appointment.status != AppointmentStatus.CONFIRMED:
            raise AppointmentDomainError(
                AppointmentErrorCode.INVALID_STATE,
                f"Cannot cancel appointment in status {appointment.status}",
            )
        if appointment.version != data.target_expected_version:
            raise AppointmentDomainError(
                AppointmentErrorCode.STALE_VERSION, "Appointment version changed"
            )

        now = datetime.now(tz=appointment.start_at.tzinfo)

        before_snapshot = await self._authoritative_snapshot(
            data.target_appointment_id, command.actor.business_id
        )

        async with self._session.begin_nested():
            await self._repo.update_allocation_status(
                command.actor.business_id,
                data.target_appointment_id,
                ResourceAllocationStatus.CANCELLED.value,
            )

            updated = await self._repo.update_appointment(
                command.actor.business_id,
                data.target_appointment_id,
                data.target_expected_version,
                {
                    "status": AppointmentStatus.CANCELLED.value,
                    "cancelled_at": now,
                    "updated_at": now,
                },
            )
            if updated is None:
                raise AppointmentDomainError(
                    AppointmentErrorCode.STALE_VERSION, "Appointment version changed"
                )

            after_snapshot = await self._authoritative_snapshot(
                data.target_appointment_id, command.actor.business_id
            )

            commit = await self._repo.insert_commit(
                {
                    "business_id": command.actor.business_id,
                    "pending_action_id": command.pending_action_id,
                    "appointment_id": data.target_appointment_id,
                    "operation": "cancel",
                    "before_snapshot": before_snapshot,
                    "after_snapshot": after_snapshot,
                    "reason_code": data.reason_code,
                }
            )

            await self._repo.force_constraints(
                set_constraints_immediate_sql(APPOINTMENT_CANCEL_PRE_COMPLETION_CONSTRAINTS)
            )
            await self._repo.force_constraints(
                _restore_deferred_sql(APPOINTMENT_CANCEL_PRE_COMPLETION_CONSTRAINTS)
            )

            await self._pa_service.complete_commit(
                CompleteCommitCommand(
                    context=committing_context,
                    committed_entity_type="appointment_commit",
                    committed_entity_id=commit.id,
                )
            )

            await self._repo.force_constraints(
                set_constraints_immediate_sql(APPOINTMENT_CANCEL_POST_COMPLETION_CONSTRAINTS)
            )
            await self._repo.force_constraints(
                _restore_deferred_sql(APPOINTMENT_CANCEL_POST_COMPLETION_CONSTRAINTS)
            )

        return AppointmentCancellationResult(
            appointment_id=data.target_appointment_id,
            appointment_commit_id=commit.id,
            cancelled_at=now,
        )

    async def create_reschedule_proposal(
        self,
        command: CreatePendingAppointmentRescheduleCommand,
    ) -> AppointmentProposalResult:
        appointment = await self._repo.get_by_business_and_id(
            command.actor.business_id,
            command.appointment_id,
        )
        if appointment is None:
            raise AppointmentDomainError(AppointmentErrorCode.NOT_FOUND, "Appointment not found")
        if appointment.status != AppointmentStatus.CONFIRMED:
            raise AppointmentDomainError(
                AppointmentErrorCode.INVALID_STATE,
                f"Cannot reschedule appointment in status {appointment.status}",
            )

        old_facts = self._appointment_to_facts(appointment)

        stub_end = command.start_at + timedelta(minutes=1)
        new_stub_facts = AppointmentFacts(
            service_id=command.service_id,
            service_name="__pending__",
            resource_id=command.resource_id or appointment.resource_id,
            resource_name="__pending__",
            start_at=command.start_at,
            end_at=stub_end,
            effective_start_at=command.start_at,
            effective_end_at=stub_end,
            duration_minutes=1,
            business_timezone="UTC",
        )

        stub_envelope = PendingAppointmentEnvelope(
            data=RescheduleAppointmentData(
                target_appointment_id=command.appointment_id,
                target_expected_version=command.expected_appointment_version,
                old_facts=old_facts,
                new_facts=new_stub_facts,
            )
        )
        resolved = await self._validation.validate_for_actor(command.actor, stub_envelope)
        assert isinstance(resolved.data, RescheduleAppointmentData)
        new_facts = resolved.data.new_facts

        pa_result = await self._pa_service.create(
            CreatePendingActionCommand(
                actor=command.actor,
                action_type=PendingActionType.APPOINTMENT,
                payload=canonical_payload_dict(resolved),
                expires_at=command.expires_at,
                idempotency_key=command.idempotency_key,
            )
        )

        if pa_result.status == "collecting_details":
            pa_result = await self._pa_service.mark_awaiting_confirmation(
                MarkAwaitingConfirmationCommand(
                    actor=command.actor,
                    action_id=pa_result.id,
                    expected_version=pa_result.version,
                )
            )

        return AppointmentProposalResult(
            pending_action_id=pa_result.id,
            version=pa_result.version,
            expires_at=pa_result.expires_at,
            confirmation_facts=ConfirmationFactsResult(
                operation="reschedule",
                service_id=new_facts.service_id,
                service_name=new_facts.service_name,
                resource_id=new_facts.resource_id,
                resource_name=new_facts.resource_name,
                start_at=new_facts.start_at.isoformat(),
                end_at=new_facts.end_at.isoformat(),
                duration_minutes=new_facts.duration_minutes,
                price=str(new_facts.price) if new_facts.price is not None else None,
                business_timezone=new_facts.business_timezone,
                target_appointment_id=command.appointment_id,
                old_facts=self._to_scheduling_facts(old_facts),
            ),
        )

    async def confirm_reschedule(
        self,
        command: ConfirmPendingAppointmentRescheduleCommand,
    ) -> AppointmentRescheduleResult:
        action = await self._pa_service._require_action(
            command.actor.business_id,
            command.pending_action_id,
        )
        await require_existing_action_permission(self._session, command.actor, action)

        envelope = PendingAppointmentEnvelope.model_validate(action.proposed_payload)
        data = envelope.data
        assert isinstance(data, RescheduleAppointmentData)
        new_facts = data.new_facts

        context = CommitResultContext(
            business_id=command.actor.business_id,
            pending_action_id=command.pending_action_id,
            expected_version=command.expected_version,
            engine="appointment_engine",
        )

        begin_result = await self._pa_service.begin_commit(BeginCommitCommand(context=context))
        committing_context = CommitResultContext(
            business_id=context.business_id,
            pending_action_id=context.pending_action_id,
            expected_version=begin_result.version,
            engine="appointment_engine",
        )

        appointment = await self._repo.lock_appointment(
            command.actor.business_id,
            data.target_appointment_id,
        )
        if appointment is None:
            raise AppointmentDomainError(
                AppointmentErrorCode.NOT_FOUND, "Target appointment not found"
            )
        if appointment.status != AppointmentStatus.CONFIRMED:
            raise AppointmentDomainError(
                AppointmentErrorCode.INVALID_STATE,
                f"Cannot reschedule appointment in status {appointment.status}",
            )
        if appointment.version != data.target_expected_version:
            raise AppointmentDomainError(
                AppointmentErrorCode.STALE_VERSION, "Appointment version changed"
            )

        await self._repo.lock_resource_schedule(
            command.actor.business_id,
            new_facts.resource_id,
        )

        now = datetime.now(tz=appointment.start_at.tzinfo)
        before_snapshot = await self._authoritative_snapshot(
            data.target_appointment_id, command.actor.business_id
        )

        overlap_exc: IntegrityError | None = None
        try:
            async with self._session.begin_nested():
                await self._repo.update_allocation_status(
                    command.actor.business_id,
                    data.target_appointment_id,
                    ResourceAllocationStatus.RELEASED.value,
                )

                updated = await self._repo.update_appointment(
                    command.actor.business_id,
                    data.target_appointment_id,
                    data.target_expected_version,
                    {
                        "service_id": new_facts.service_id,
                        "resource_id": new_facts.resource_id,
                        "start_at": new_facts.start_at,
                        "end_at": new_facts.end_at,
                        "effective_start_at": new_facts.effective_start_at,
                        "effective_end_at": new_facts.effective_end_at,
                        "service_name_snapshot": new_facts.service_name,
                        "resource_name_snapshot": new_facts.resource_name,
                        "duration_minutes_snapshot": new_facts.duration_minutes,
                        "buffer_before_minutes_snapshot": new_facts.buffer_before_minutes,
                        "buffer_after_minutes_snapshot": new_facts.buffer_after_minutes,
                        "price_snapshot": (
                            Decimal(str(new_facts.price)) if new_facts.price is not None else None
                        ),
                        "business_timezone_snapshot": new_facts.business_timezone,
                        "rescheduled_at": now,
                        "updated_at": now,
                    },
                )
                if updated is None:
                    raise AppointmentDomainError(
                        AppointmentErrorCode.STALE_VERSION, "Appointment version changed"
                    )

                await self._repo.insert_allocation(
                    {
                        "business_id": command.actor.business_id,
                        "resource_id": new_facts.resource_id,
                        "appointment_id": data.target_appointment_id,
                        "pending_action_id": appointment.pending_action_id,
                        "allocation_type": ResourceAllocationType.APPOINTMENT.value,
                        "status": ResourceAllocationStatus.ACTIVE.value,
                        "source": ResourceAllocationSource.CUSTOMER_CONVERSATION.value,
                        "effective_start_at": new_facts.effective_start_at,
                        "effective_end_at": new_facts.effective_end_at,
                        "idempotency_key": f"resched-{command.pending_action_id}",
                    }
                )

                after_snapshot = await self._authoritative_snapshot(
                    data.target_appointment_id, command.actor.business_id
                )

                commit = await self._repo.insert_commit(
                    {
                        "business_id": command.actor.business_id,
                        "pending_action_id": command.pending_action_id,
                        "appointment_id": data.target_appointment_id,
                        "operation": "reschedule",
                        "before_snapshot": before_snapshot,
                        "after_snapshot": after_snapshot,
                    }
                )

                await self._repo.force_constraints(
                    set_constraints_immediate_sql(APPOINTMENT_RESCHEDULE_PRE_COMPLETION_CONSTRAINTS)
                )
                await self._repo.force_constraints(
                    _restore_deferred_sql(APPOINTMENT_RESCHEDULE_PRE_COMPLETION_CONSTRAINTS)
                )

                await self._pa_service.complete_commit(
                    CompleteCommitCommand(
                        context=committing_context,
                        committed_entity_type="appointment_commit",
                        committed_entity_id=commit.id,
                    )
                )

                await self._repo.force_constraints(
                    set_constraints_immediate_sql(
                        APPOINTMENT_RESCHEDULE_POST_COMPLETION_CONSTRAINTS
                    )
                )
                await self._repo.force_constraints(
                    _restore_deferred_sql(APPOINTMENT_RESCHEDULE_POST_COMPLETION_CONSTRAINTS)
                )
        except IntegrityError as exc:
            if (
                getattr(exc.orig, "sqlstate", None) == _OVERLAP_SQLSTATE
                and _pg_constraint_name(exc) == _OVERLAP_CONSTRAINT
            ):
                overlap_exc = exc
            else:
                raise

        if overlap_exc is not None:
            await self._pa_service.fail_commit(
                FailCommitCommand(
                    context=committing_context,
                    error_code="resource_unavailable",
                    retryable=True,
                )
            )
            raise AppointmentDomainError(
                AppointmentErrorCode.SLOT_CONFLICT,
                "New time slot conflicts with existing allocation",
            )

        assert updated is not None
        return AppointmentRescheduleResult(
            appointment_id=data.target_appointment_id,
            appointment_commit_id=commit.id,
            version=updated.version,
            resource_id=new_facts.resource_id,
            resource_name=new_facts.resource_name,
            start_at=new_facts.start_at,
            end_at=new_facts.end_at,
        )

    def _appointment_to_facts(self, appointment: object) -> AppointmentFacts:
        return AppointmentFacts(
            service_id=appointment.service_id,  # type: ignore[attr-defined]
            service_name=appointment.service_name_snapshot,  # type: ignore[attr-defined]
            resource_id=appointment.resource_id,  # type: ignore[attr-defined]
            resource_name=appointment.resource_name_snapshot,  # type: ignore[attr-defined]
            start_at=appointment.start_at,  # type: ignore[attr-defined]
            end_at=appointment.end_at,  # type: ignore[attr-defined]
            effective_start_at=appointment.effective_start_at,  # type: ignore[attr-defined]
            effective_end_at=appointment.effective_end_at,  # type: ignore[attr-defined]
            duration_minutes=appointment.duration_minutes_snapshot,  # type: ignore[attr-defined]
            buffer_before_minutes=appointment.buffer_before_minutes_snapshot,  # type: ignore[attr-defined]
            buffer_after_minutes=appointment.buffer_after_minutes_snapshot,  # type: ignore[attr-defined]
            price=appointment.price_snapshot,  # type: ignore[attr-defined]
            business_timezone=appointment.business_timezone_snapshot,  # type: ignore[attr-defined]
        )

    async def _authoritative_snapshot(self, appointment_id: int, business_id: int) -> object:
        from sqlalchemy import text

        result = await self._session.scalar(
            text(
                "SELECT appointment_authoritative_snapshot(a) "
                "FROM appointments a WHERE a.id = :id AND a.business_id = :bid"
            ),
            {"id": appointment_id, "bid": business_id},
        )
        return result

    def _to_scheduling_facts(self, facts: AppointmentFacts) -> ConfirmationSchedulingFacts:
        return ConfirmationSchedulingFacts(
            service_id=facts.service_id,
            service_name=facts.service_name,
            resource_id=facts.resource_id,
            resource_name=facts.resource_name,
            start_at=facts.start_at.isoformat(),
            end_at=facts.end_at.isoformat(),
            duration_minutes=facts.duration_minutes,
            price=str(facts.price) if facts.price is not None else None,
            business_timezone=facts.business_timezone,
        )

    async def _resolve_envelope(
        self,
        command: CreatePendingAppointmentCommand,
    ) -> PendingAppointmentEnvelope:
        if command.resource_id is None:
            raise ValueError("resource_id is required")
        stub_end = command.start_at + timedelta(minutes=1)
        stub_envelope = PendingAppointmentEnvelope(
            data=CreateAppointmentData(
                facts=AppointmentFacts(
                    service_id=command.service_id,
                    service_name="__pending__",
                    resource_id=command.resource_id,
                    resource_name="__pending__",
                    start_at=command.start_at,
                    end_at=stub_end,
                    effective_start_at=command.start_at,
                    effective_end_at=stub_end,
                    duration_minutes=1,
                    business_timezone="UTC",
                ),
                customer_name=command.customer_name,
                customer_phone=command.customer_phone,
                reason=command.reason,
                call_id=command.call_id,
            )
        )
        return await self._validation.validate_for_actor(
            command.actor,
            stub_envelope,
        )

    def _to_confirmation_facts(self, facts: AppointmentFacts) -> ConfirmationFactsResult:
        return ConfirmationFactsResult(
            operation="create",
            service_id=facts.service_id,
            service_name=facts.service_name,
            resource_id=facts.resource_id,
            resource_name=facts.resource_name,
            start_at=facts.start_at.isoformat(),
            end_at=facts.end_at.isoformat(),
            duration_minutes=facts.duration_minutes,
            price=str(facts.price) if facts.price is not None else None,
            business_timezone=facts.business_timezone,
        )

    def _replay_result(
        self,
        appointment: object,
        authoritative_version: int,
    ) -> PreCommitAppointmentSuccess:
        return PreCommitAppointmentSuccess(
            appointment=AppointmentConfirmationResult(
                appointment_id=appointment.id,  # type: ignore[attr-defined]
                pending_action_id=appointment.pending_action_id,  # type: ignore[attr-defined]
                service_id=appointment.service_id,  # type: ignore[attr-defined]
                service_name=appointment.service_name_snapshot,  # type: ignore[attr-defined]
                resource_id=appointment.resource_id,  # type: ignore[attr-defined]
                resource_name=appointment.resource_name_snapshot,  # type: ignore[attr-defined]
                start_at=appointment.start_at,  # type: ignore[attr-defined]
                end_at=appointment.end_at,  # type: ignore[attr-defined]
                price=appointment.price_snapshot,  # type: ignore[attr-defined]
                business_timezone=appointment.business_timezone_snapshot,  # type: ignore[attr-defined]
            ),
            pending_action_version=authoritative_version,
        )
