"""D3 appointment create-and-confirm application transaction."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCommand,
    CreatePendingAppointmentCommand,
)
from fonely.domain.appointments.commit_contract import (
    APPOINTMENT_CREATE_POST_COMPLETION_CONSTRAINTS,
    APPOINTMENT_CREATE_PRE_COMPLETION_CONSTRAINTS,
    set_constraints_immediate_sql,
)
from fonely.domain.appointments.results import (
    AppointmentCommitFailureCode,
    AppointmentConfirmationResult,
    AppointmentProposalResult,
    ConfirmationFactsResult,
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
    CreateAppointmentData,
    PendingAppointmentEnvelope,
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
        facts = resolved_envelope.data.facts

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

        return AppointmentProposalResult(
            pending_action_id=pa_result.id,
            version=pa_result.version,
            expires_at=pa_result.expires_at,
            confirmation_facts=self._to_confirmation_facts(facts),
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

        savepoint = await self._session.begin_nested()
        try:
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

            await savepoint.commit()

        except BaseException as exc:
            if savepoint.is_active:
                try:
                    await savepoint.rollback()
                except BaseException as rollback_exc:
                    raise rollback_exc from exc

            if isinstance(exc, IntegrityError) and (
                getattr(exc.orig, "sqlstate", None) == _OVERLAP_SQLSTATE
                and _pg_constraint_name(exc) == _OVERLAP_CONSTRAINT
            ):
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
            raise

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

    async def _resolve_envelope(
        self,
        command: CreatePendingAppointmentCommand,
    ) -> PendingAppointmentEnvelope:
        stub_end = command.start_at + timedelta(minutes=1)
        stub_envelope = PendingAppointmentEnvelope(
            data=CreateAppointmentData(
                facts=AppointmentFacts(
                    service_id=command.service_id,
                    service_name="__pending__",
                    resource_id=command.resource_id or 1,
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
