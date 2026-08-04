"""Application service for the deterministic PendingAction lifecycle."""

from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.validators import utcnow
from fonely.domain.appointments.validation import AppointmentValidationPort
from fonely.domain.pending_actions.commands import (
    ActorContext,
    BeginCommitCommand,
    BulkExpirePendingActionsCommand,
    CancelPendingActionCommand,
    CommitResultContext,
    CompleteCommitCommand,
    CreatePendingActionCommand,
    ExpirePendingActionCommand,
    FailCommitCommand,
    GetActivePendingActionQuery,
    GetPendingActionQuery,
    InternalGetActivePendingActionQuery,
    InternalGetPendingActionQuery,
    MarkAwaitingConfirmationCommand,
    RejectPendingActionCommand,
    RevisePendingActionCommand,
)
from fonely.domain.pending_actions.errors import (
    CommitEntityConflictError,
    InvalidStateTransitionError,
    PendingActionConcurrencyError,
    PendingActionExpiredError,
    PendingActionIdempotencyConflictError,
    PendingActionNotFoundError,
    TrustedCommitContextError,
)
from fonely.domain.pending_actions.payloads import (
    OwnerStockUpdateEnvelope,
    PayloadEnvelope,
    PendingAppointmentEnvelope,
    PendingOrderEnvelope,
    validate_payload,
)
from fonely.domain.pending_actions.results import BulkExpiryResult, PendingActionResult
from fonely.domain.pending_actions.snapshots import (
    canonical_payload_dict,
    confirmation_snapshot,
    payload_digest,
)
from fonely.domain.pending_actions.transitions import (
    assert_revision_allowed,
    assert_transition_allowed,
)
from fonely.models.enums import PendingActionStatus, PendingActionType
from fonely.models.schema import (
    Appointment,
    AppointmentCommit,
    Business,
    InventoryMovement,
    Order,
    PendingAction,
    Product,
)
from fonely.repositories.pending_actions import PendingActionRepository
from fonely.services.authorization import (
    require_action_permission,
    require_existing_action_permission,
)

MAX_EXPIRY_HORIZON = timedelta(hours=24)

type CommitEntityModel = (
    type[Order] | type[Appointment] | type[AppointmentCommit] | type[InventoryMovement]
)

_COMMIT_POLICY: dict[PendingActionType, tuple[str, str, CommitEntityModel]] = {
    PendingActionType.ORDER: ("order_engine", "order", Order),
    PendingActionType.OWNER_STOCK_UPDATE: (
        "inventory_engine",
        "inventory_update",
        InventoryMovement,
    ),
}

_SAFE_COMMIT_MESSAGES: dict[str, str] = {
    "temporary_conflict": "The action could not be completed yet. Please confirm again.",
    "insufficient_stock": "The requested quantity is no longer available.",
    "invalid_product": "One or more products are no longer available.",
    "resource_unavailable": "The requested time is no longer available.",
    "transaction_failed": "The action could not be completed safely.",
}


class PendingActionService:
    """Orchestrates PendingAction domain rules within a caller-owned transaction."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        appointment_validation: AppointmentValidationPort | None = None,
    ) -> None:
        self._session = session
        self._repo = PendingActionRepository(session)
        self._appointment_validation = appointment_validation

    async def find_idempotent_action(
        self,
        actor: ActorContext,
        idempotency_key: str,
    ) -> PendingAction | None:
        await self._require_business(actor.business_id)
        await require_action_permission(self._session, actor, PendingActionType.APPOINTMENT)
        action = await self._repo.get_by_idempotency_key(actor.business_id, idempotency_key)
        if action is not None:
            await require_existing_action_permission(self._session, actor, action)
            self._validated_stored_payload(action)
        return action

    async def validated_stored_appointment_envelope(
        self, action: PendingAction
    ) -> PendingAppointmentEnvelope:
        payload = self._validated_stored_payload(action)
        if not isinstance(payload, PendingAppointmentEnvelope):
            raise PendingActionIdempotencyConflictError(
                "Idempotency key already used for a different action"
            )
        return payload

    async def create(self, command: CreatePendingActionCommand) -> PendingActionResult:
        await self._require_business(command.actor.business_id)
        await require_action_permission(self._session, command.actor, command.action_type)
        proposed_payload = validate_payload(
            command.action_type,
            command.payload_schema_version,
            command.payload,
        )
        existing = await self._repo.get_by_idempotency_key(
            command.actor.business_id,
            command.idempotency_key,
        )
        if existing is not None:
            stored_payload = self._validated_stored_payload(existing)
            await self._validate_idempotent_retry(
                command.actor,
                proposed_payload,
                stored_payload,
            )
            self._assert_idempotent_equivalence(
                existing,
                action_type=command.action_type,
                schema_version=command.payload_schema_version,
                digest=payload_digest(stored_payload),
                expires_at=command.expires_at,
                session_id=command.actor.session_id,
            )
            await self._validate_stored_payload_ownership(
                command.actor.business_id,
                stored_payload,
            )
            return self._to_result(existing)

        payload = await self._validate_actor_appointment_payload(command.actor, proposed_payload)
        digest = payload_digest(payload)
        self._validate_expiry(command.expires_at, utcnow())
        await self._validate_new_payload_products(command.actor.business_id, payload)
        create_values = {
            "business_id": command.actor.business_id,
            "session_id": command.actor.session_id,
            "action_type": command.action_type.value,
            "payload_schema_version": command.payload_schema_version,
            "proposed_payload": canonical_payload_dict(payload),
            "payload_digest": digest,
            "confirmation_snapshot": None,
            "status": PendingActionStatus.COLLECTING_DETAILS.value,
            "expires_at": command.expires_at,
            "idempotency_key": command.idempotency_key,
            "initiated_by": command.actor.normalized_phone,
            "confirmed_by": None,
            "committed_entity_type": None,
            "committed_entity_id": None,
            "commit_error_code": None,
            "commit_error_message": None,
            "rejection_reason_code": None,
            "version": 1,
        }
        action = await self._repo.insert_idempotent(create_values)
        if action is not None:
            return self._to_result(action)
        existing = await self._repo.get_by_idempotency_key(
            command.actor.business_id,
            command.idempotency_key,
        )
        if existing is None:
            raise PendingActionConcurrencyError(
                "Conflicting pending action was not visible after insert"
            )
        stored_payload = self._validated_stored_payload(existing)
        await self._validate_idempotent_retry(
            command.actor,
            proposed_payload,
            stored_payload,
        )
        self._assert_idempotent_equivalence(
            existing,
            action_type=command.action_type,
            schema_version=command.payload_schema_version,
            digest=payload_digest(stored_payload),
            expires_at=command.expires_at,
            session_id=command.actor.session_id,
        )
        return self._to_result(existing)

    async def get(self, query: GetPendingActionQuery) -> PendingActionResult:
        action = await self._require_action(query.actor.business_id, query.action_id)
        await require_existing_action_permission(self._session, query.actor, action)
        return self._to_result(action)

    async def get_active(self, query: GetActivePendingActionQuery) -> PendingActionResult | None:
        action = await self._repo.get_active_for_session(
            query.actor.business_id,
            query.session_id,
            utcnow(),
            query.action_type,
        )
        if action is None:
            return None
        await require_existing_action_permission(self._session, query.actor, action)
        payload = await self._validate_actor_appointment_payload(
            query.actor,
            self._validated_stored_payload(action),
        )
        self._assert_authoritative_payload_unchanged(action, payload)
        await self._validate_stored_payload_ownership(query.actor.business_id, payload)
        return self._to_result(action)

    async def internal_get(self, query: InternalGetPendingActionQuery) -> PendingActionResult:
        return self._to_result(await self._require_action(query.business_id, query.action_id))

    async def internal_get_active(
        self,
        query: InternalGetActivePendingActionQuery,
    ) -> PendingActionResult | None:
        action = await self._repo.get_active_for_session(
            query.business_id,
            query.session_id,
            query.now,
            query.action_type,
        )
        if action is None:
            return None
        payload = await self._validate_stored_appointment_payload(
            query.business_id,
            self._validated_stored_payload(action),
        )
        self._assert_authoritative_payload_unchanged(action, payload)
        await self._validate_stored_payload_ownership(query.business_id, payload)
        return self._to_result(action)

    async def revise(self, command: RevisePendingActionCommand) -> PendingActionResult:
        action = await self._require_action_without_appointment_revalidation(
            command.actor.business_id,
            command.action_id,
        )
        await require_existing_action_permission(self._session, command.actor, action)
        assert_revision_allowed(PendingActionStatus(action.status))
        action_type = PendingActionType(action.action_type)
        payload = validate_payload(
            action_type,
            command.payload_schema_version,
            command.payload,
        )
        payload = await self._validate_actor_appointment_payload(command.actor, payload)
        await self._validate_new_payload_products(command.actor.business_id, payload)
        updated = await self._repo.conditional_update(
            business_id=command.actor.business_id,
            action_id=command.action_id,
            expected_version=command.expected_version,
            expected_status=PendingActionStatus(action.status),
            values={
                "payload_schema_version": command.payload_schema_version,
                "proposed_payload": canonical_payload_dict(payload),
                "payload_digest": payload_digest(payload),
                "confirmation_snapshot": None,
                "confirmed_by": None,
                "confirmed_at": None,
                "committed_entity_type": None,
                "committed_entity_id": None,
                "commit_error_code": None,
                "commit_error_message": None,
                "rejection_reason_code": None,
                "status": PendingActionStatus.COLLECTING_DETAILS.value,
            },
        )
        return self._to_result(
            await self._resolve_update(
                updated,
                command.actor.business_id,
                action,
                command.expected_version,
                PendingActionStatus.COLLECTING_DETAILS,
            )
        )

    async def mark_awaiting_confirmation(
        self,
        command: MarkAwaitingConfirmationCommand,
    ) -> PendingActionResult:
        action = await self._require_action(command.actor.business_id, command.action_id)
        await require_existing_action_permission(self._session, command.actor, action)
        self._assert_not_expired(action, utcnow())
        assert_transition_allowed(
            PendingActionStatus(action.status),
            PendingActionStatus.AWAITING_CONFIRMATION,
        )
        payload = self._validated_stored_payload(action)
        payload = await self._validate_actor_appointment_payload(command.actor, payload)
        if payload_digest(payload) != action.payload_digest:
            raise PendingActionIdempotencyConflictError(
                "Authoritative appointment facts changed; revise the proposal"
            )
        await self._validate_new_payload_products(command.actor.business_id, payload)
        updated = await self._repo.conditional_update(
            business_id=command.actor.business_id,
            action_id=command.action_id,
            expected_version=command.expected_version,
            expected_status=PendingActionStatus.COLLECTING_DETAILS,
            values={
                "confirmation_snapshot": confirmation_snapshot(payload),
                "status": PendingActionStatus.AWAITING_CONFIRMATION.value,
            },
            expires_after=utcnow(),
        )
        return self._to_result(
            await self._resolve_update(
                updated,
                command.actor.business_id,
                action,
                command.expected_version,
                PendingActionStatus.AWAITING_CONFIRMATION,
            )
        )

    async def begin_commit(self, command: BeginCommitCommand) -> PendingActionResult:
        context = command.context
        action = await self._require_action(context.business_id, context.pending_action_id)
        self._assert_trusted_engine(action, context)
        payload = await self._validate_stored_appointment_payload(
            context.business_id,
            self._validated_stored_payload(action),
        )
        if payload_digest(payload) != action.payload_digest:
            raise PendingActionIdempotencyConflictError(
                "Authoritative appointment facts changed; revise the proposal"
            )
        await self._validate_new_payload_products(context.business_id, payload)
        now = utcnow()
        self._assert_not_expired(action, now)
        if action.confirmation_snapshot != confirmation_snapshot(payload):
            raise InvalidStateTransitionError(
                PendingActionStatus(action.status),
                PendingActionStatus.COMMITTING,
            )
        assert_transition_allowed(
            PendingActionStatus(action.status),
            PendingActionStatus.COMMITTING,
        )
        updated = await self._repo.conditional_update(
            business_id=context.business_id,
            action_id=context.pending_action_id,
            expected_version=context.expected_version,
            expected_status=PendingActionStatus.AWAITING_CONFIRMATION,
            values={
                "status": PendingActionStatus.COMMITTING.value,
                "commit_error_code": None,
                "commit_error_message": None,
            },
            expires_after=now,
        )
        return self._to_result(
            await self._resolve_update(
                updated,
                context.business_id,
                action,
                context.expected_version,
                PendingActionStatus.COMMITTING,
                now,
            )
        )

    async def complete_commit(self, command: CompleteCommitCommand) -> PendingActionResult:
        context = command.context
        action = await self._require_action_without_appointment_revalidation(
            context.business_id,
            context.pending_action_id,
        )
        payload = self._validated_stored_payload(action)
        expected_entity_type, entity_model = self._assert_trusted_engine(action, context)
        if command.committed_entity_type != expected_entity_type:
            raise TrustedCommitContextError("Committed entity type does not match action type")
        if action.status == PendingActionStatus.CONFIRMED.value:
            if (
                action.committed_entity_type == command.committed_entity_type
                and action.committed_entity_id == command.committed_entity_id
            ):
                return self._to_result(action)
            raise CommitEntityConflictError("Action already confirmed with a different entity")
        await self._require_committed_entity(
            entity_model,
            context.business_id,
            command.committed_entity_id,
            context.pending_action_id,
        )
        await self._validate_completion_evidence(
            context.business_id,
            payload,
            command.committed_entity_type,
            command.committed_entity_id,
        )
        assert_transition_allowed(
            PendingActionStatus(action.status),
            PendingActionStatus.CONFIRMED,
        )
        now = utcnow()
        updated = await self._repo.conditional_update(
            business_id=context.business_id,
            action_id=context.pending_action_id,
            expected_version=context.expected_version,
            expected_status=PendingActionStatus.COMMITTING,
            values={
                "status": PendingActionStatus.CONFIRMED.value,
                "committed_entity_type": command.committed_entity_type,
                "committed_entity_id": command.committed_entity_id,
                "confirmed_by": context.engine,
                "confirmed_at": now,
                "commit_error_code": None,
                "commit_error_message": None,
            },
        )
        return self._to_result(
            await self._resolve_complete_commit(
                updated,
                context.business_id,
                action,
                context.expected_version,
                command.committed_entity_type,
                command.committed_entity_id,
            )
        )

    async def fail_commit(self, command: FailCommitCommand) -> PendingActionResult:
        context = command.context
        action = await self._require_action_without_appointment_revalidation(
            context.business_id,
            context.pending_action_id,
        )
        self._assert_trusted_engine(action, context)
        requested = (
            PendingActionStatus.AWAITING_CONFIRMATION
            if command.retryable
            else PendingActionStatus.REJECTED
        )
        assert_transition_allowed(PendingActionStatus(action.status), requested)
        updated = await self._repo.conditional_update(
            business_id=context.business_id,
            action_id=context.pending_action_id,
            expected_version=context.expected_version,
            expected_status=PendingActionStatus.COMMITTING,
            values={
                "status": requested.value,
                "commit_error_code": command.error_code,
                "commit_error_message": _SAFE_COMMIT_MESSAGES[command.error_code],
                "rejection_reason_code": None if command.retryable else command.error_code,
            },
        )
        return self._to_result(
            await self._resolve_update(
                updated,
                context.business_id,
                action,
                context.expected_version,
                requested,
            )
        )

    async def reject(self, command: RejectPendingActionCommand) -> PendingActionResult:
        action = await self._require_action_without_appointment_revalidation(
            command.actor.business_id,
            command.action_id,
        )
        await require_existing_action_permission(self._session, command.actor, action)
        assert_transition_allowed(
            PendingActionStatus(action.status),
            PendingActionStatus.REJECTED,
        )
        updated = await self._repo.conditional_update(
            business_id=command.actor.business_id,
            action_id=command.action_id,
            expected_version=command.expected_version,
            expected_status=PendingActionStatus.AWAITING_CONFIRMATION,
            values={
                "status": PendingActionStatus.REJECTED.value,
                "rejection_reason_code": command.reason_code,
            },
        )
        return self._to_result(
            await self._resolve_update(
                updated,
                command.actor.business_id,
                action,
                command.expected_version,
                PendingActionStatus.REJECTED,
            )
        )

    async def cancel(self, command: CancelPendingActionCommand) -> PendingActionResult:
        action = await self._require_action_without_appointment_revalidation(
            command.actor.business_id,
            command.action_id,
        )
        await require_existing_action_permission(self._session, command.actor, action)
        if action.status == PendingActionStatus.CANCELLED.value:
            return self._to_result(action)
        assert_transition_allowed(
            PendingActionStatus(action.status),
            PendingActionStatus.CANCELLED,
        )
        updated = await self._repo.conditional_update(
            business_id=command.actor.business_id,
            action_id=command.action_id,
            expected_version=command.expected_version,
            expected_status=PendingActionStatus(action.status),
            values={"status": PendingActionStatus.CANCELLED.value},
        )
        return self._to_result(
            await self._resolve_update(
                updated,
                command.actor.business_id,
                action,
                command.expected_version,
                PendingActionStatus.CANCELLED,
            )
        )

    async def expire(self, command: ExpirePendingActionCommand) -> PendingActionResult:
        action = await self._require_action_without_appointment_revalidation(
            command.business_id,
            command.action_id,
        )
        if action.status == PendingActionStatus.EXPIRED.value:
            return self._to_result(action)
        if action.expires_at > command.now:
            raise PendingActionExpiredError("Action has not reached its expiry time")
        assert_transition_allowed(
            PendingActionStatus(action.status),
            PendingActionStatus.EXPIRED,
        )
        updated = await self._repo.conditional_update(
            business_id=command.business_id,
            action_id=command.action_id,
            expected_version=command.expected_version,
            expected_status=PendingActionStatus(action.status),
            values={"status": PendingActionStatus.EXPIRED.value},
        )
        return self._to_result(
            await self._resolve_update(
                updated,
                command.business_id,
                action,
                command.expected_version,
                PendingActionStatus.EXPIRED,
            )
        )

    async def bulk_expire(
        self,
        command: BulkExpirePendingActionsCommand,
    ) -> BulkExpiryResult:
        ids = await self._repo.bulk_expire(now=command.now, batch_size=command.batch_size)
        return BulkExpiryResult(expired_ids=ids, count=len(ids))

    async def _require_business(self, business_id: int) -> None:
        exists = await self._session.scalar(select(Business.id).where(Business.id == business_id))
        if exists is None:
            raise PendingActionNotFoundError("Business not found")

    async def _require_action(self, business_id: int, action_id: int) -> PendingAction:
        action = await self._require_action_without_appointment_revalidation(business_id, action_id)
        if action.status in ("confirmed", "rejected", "cancelled", "expired"):
            return action
        payload = await self._validate_stored_appointment_payload(
            business_id,
            self._validated_stored_payload(action),
        )
        self._assert_authoritative_payload_unchanged(action, payload)
        await self._validate_stored_payload_ownership(business_id, payload)
        return action

    async def _require_action_without_appointment_revalidation(
        self,
        business_id: int,
        action_id: int,
    ) -> PendingAction:
        action = await self._repo.get_by_id(business_id, action_id)
        if action is None:
            raise PendingActionNotFoundError("Pending action not found")
        payload = self._validated_stored_payload(action)
        await self._validate_stored_payload_ownership(business_id, payload)
        return action

    @staticmethod
    def _payload_product_ids(payload: PayloadEnvelope) -> set[int]:
        if isinstance(payload, PendingOrderEnvelope):
            return {line.product_id for line in payload.data.lines}
        if isinstance(payload, OwnerStockUpdateEnvelope):
            return {payload.data.product_id}
        return set()

    async def _validate_new_payload_products(
        self,
        business_id: int,
        payload: PayloadEnvelope,
    ) -> None:
        await self._validate_product_ids(business_id, payload, active_only=True)

    async def _validate_stored_payload_ownership(
        self,
        business_id: int,
        payload: PayloadEnvelope,
    ) -> None:
        await self._validate_product_ids(business_id, payload, active_only=False)

    async def _validate_product_ids(
        self,
        business_id: int,
        payload: PayloadEnvelope,
        *,
        active_only: bool,
    ) -> None:
        product_ids = self._payload_product_ids(payload)
        if not product_ids:
            return
        statement = select(Product.id).where(
            Product.business_id == business_id,
            Product.id.in_(sorted(product_ids)),
        )
        if active_only:
            statement = statement.where(Product.is_active.is_(True))
        owned_ids = set((await self._session.scalars(statement)).all())
        if owned_ids != product_ids:
            raise PendingActionNotFoundError("One or more products were not found")

    def _assert_trusted_engine(
        self,
        action: PendingAction,
        context: CommitResultContext,
    ) -> tuple[str, CommitEntityModel]:
        action_type = PendingActionType(action.action_type)
        policy: tuple[str, str, CommitEntityModel] | None
        if action_type == PendingActionType.APPOINTMENT:
            payload = self._validated_stored_payload(action)
            assert isinstance(payload, PendingAppointmentEnvelope)
            if payload.data.operation == "create":
                policy = ("appointment_engine", "appointment", Appointment)
            else:
                policy = ("appointment_engine", "appointment_commit", AppointmentCommit)
        else:
            policy = _COMMIT_POLICY.get(action_type)
        if policy is None:
            raise TrustedCommitContextError("No commit engine is implemented for this action type")
        expected_engine, entity_type, entity_model = policy
        if context.engine != expected_engine:
            raise TrustedCommitContextError("Commit engine does not match action type")
        return entity_type, entity_model

    async def _require_committed_entity(
        self,
        entity_model: CommitEntityModel,
        business_id: int,
        entity_id: int,
        pending_action_id: int,
    ) -> None:
        statement = select(entity_model.id).where(
            entity_model.business_id == business_id,
            entity_model.id == entity_id,
            entity_model.pending_action_id == pending_action_id,
        )
        if await self._session.scalar(statement) is None:
            raise TrustedCommitContextError("Committed entity was not found")

    async def _validate_actor_appointment_payload(
        self,
        actor: ActorContext,
        payload: PayloadEnvelope,
    ) -> PayloadEnvelope:
        if not isinstance(payload, PendingAppointmentEnvelope):
            return payload
        if self._appointment_validation is None:
            raise TrustedCommitContextError("Appointment validation port is not configured")
        validated = await self._appointment_validation.validate_for_actor(actor, payload)
        reconstructed = self._revalidate_appointment_envelope(validated)
        return self._assert_authoritative_appointment_payload(actor.business_id, reconstructed)

    async def _validate_stored_appointment_payload(
        self,
        business_id: int,
        payload: PayloadEnvelope,
    ) -> PayloadEnvelope:
        if not isinstance(payload, PendingAppointmentEnvelope):
            return payload
        if self._appointment_validation is None:
            raise TrustedCommitContextError("Appointment validation port is not configured")
        validated = await self._appointment_validation.validate_stored(business_id, payload)
        reconstructed = self._revalidate_appointment_envelope(validated)
        return self._assert_authoritative_appointment_payload(business_id, reconstructed)

    @staticmethod
    def _revalidate_appointment_envelope(
        payload: object,
    ) -> PendingAppointmentEnvelope:
        try:
            raw_payload = getattr(payload, "__dict__", None)
            if isinstance(raw_payload, Mapping):
                candidate = dict(raw_payload)
            elif isinstance(payload, Mapping):
                candidate = dict(payload)
            else:
                raise TypeError("Appointment validation port returned an invalid payload")
            return PendingAppointmentEnvelope.model_validate(candidate)
        except (TypeError, ValidationError) as error:
            raise TrustedCommitContextError(
                "Appointment validation port returned an invalid payload"
            ) from error

    async def _validate_idempotent_retry(
        self,
        actor: ActorContext,
        proposed: PayloadEnvelope,
        stored: PayloadEnvelope,
    ) -> None:
        if not isinstance(stored, PendingAppointmentEnvelope):
            if payload_digest(proposed) != payload_digest(stored):
                raise PendingActionIdempotencyConflictError(
                    "Idempotency key already used for a different action"
                )
            return
        if not isinstance(proposed, PendingAppointmentEnvelope):
            raise PendingActionIdempotencyConflictError(
                "Idempotency key already used for a different action"
            )
        if self._appointment_validation is None:
            raise TrustedCommitContextError("Appointment validation port is not configured")
        validated_proposed = self._revalidate_appointment_envelope(proposed)
        validated_stored = self._revalidate_appointment_envelope(stored)
        await self._appointment_validation.validate_idempotent_retry(
            actor, validated_proposed, validated_stored
        )

    async def _validate_completion_evidence(
        self,
        business_id: int,
        payload: PayloadEnvelope,
        entity_type: str,
        entity_id: int,
    ) -> None:
        if not isinstance(payload, PendingAppointmentEnvelope):
            return
        if self._appointment_validation is None:
            raise TrustedCommitContextError("Appointment validation port is not configured")
        validated = self._revalidate_appointment_envelope(payload)
        await self._appointment_validation.validate_completion_evidence(
            business_id,
            validated,
            entity_type,
            entity_id,
        )

    @staticmethod
    def _assert_authoritative_appointment_payload(
        business_id: int,
        payload: PendingAppointmentEnvelope,
    ) -> PendingAppointmentEnvelope:
        if business_id <= 0 or payload.action_type != PendingActionType.APPOINTMENT:
            raise TrustedCommitContextError("Invalid authoritative appointment payload")
        return payload

    @staticmethod
    def _assert_authoritative_payload_unchanged(
        action: PendingAction,
        payload: PayloadEnvelope,
    ) -> None:
        if payload_digest(payload) != action.payload_digest:
            raise PendingActionIdempotencyConflictError(
                "Authoritative appointment facts changed; revise the proposal"
            )

    def _validated_stored_payload(self, action: PendingAction) -> PayloadEnvelope:
        action_type = PendingActionType(action.action_type)
        payload = validate_payload(
            action_type,
            action.payload_schema_version,
            action.proposed_payload,
        )
        if payload_digest(payload) != action.payload_digest:
            raise PendingActionIdempotencyConflictError("Stored payload digest mismatch")
        return payload

    @staticmethod
    def _assert_idempotent_equivalence(
        existing: PendingAction,
        *,
        action_type: PendingActionType,
        schema_version: int,
        digest: str,
        expires_at: datetime,
        session_id: str | None,
    ) -> None:
        equivalent = (
            existing.action_type == action_type.value
            and existing.payload_schema_version == schema_version
            and existing.payload_digest == digest
            and existing.expires_at == expires_at
            and existing.session_id == session_id
        )
        if not equivalent:
            raise PendingActionIdempotencyConflictError(
                "Idempotency key already used for a different action"
            )

    @staticmethod
    def _validate_expiry(expires_at: datetime, now: datetime) -> None:
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        if expires_at <= now:
            raise PendingActionExpiredError("expires_at must be in the future")
        if expires_at > now + MAX_EXPIRY_HORIZON:
            raise ValueError("expires_at exceeds maximum pending-action horizon")

    @staticmethod
    def _assert_not_expired(action: PendingAction, now: datetime) -> None:
        if action.expires_at <= now:
            raise PendingActionExpiredError("Pending action has expired")

    async def _resolve_complete_commit(
        self,
        updated: PendingAction | None,
        business_id: int,
        prior: PendingAction,
        expected_version: int,
        committed_entity_type: str,
        committed_entity_id: int,
    ) -> PendingAction:
        if updated is not None:
            return updated
        current = await self._repo.get_by_id(business_id, prior.id)
        if current is None:
            raise PendingActionNotFoundError("Pending action not found")
        if current.status == PendingActionStatus.CONFIRMED.value:
            if (
                current.committed_entity_type == committed_entity_type
                and current.committed_entity_id == committed_entity_id
            ):
                return current
            raise CommitEntityConflictError("Action already confirmed with a different entity")
        if current.version != expected_version:
            raise PendingActionConcurrencyError("Pending action version is stale")
        raise InvalidStateTransitionError(
            PendingActionStatus(current.status),
            PendingActionStatus.CONFIRMED,
        )

    async def _resolve_update(
        self,
        updated: PendingAction | None,
        business_id: int,
        prior: PendingAction,
        expected_version: int,
        requested_status: PendingActionStatus,
        now: datetime | None = None,
    ) -> PendingAction:
        if updated is not None:
            return updated
        current = await self._repo.get_by_id(business_id, prior.id)
        if current is None:
            raise PendingActionNotFoundError("Pending action not found")
        if now is not None and current.expires_at <= now:
            raise PendingActionExpiredError("Pending action has expired")
        if current.version != expected_version:
            raise PendingActionConcurrencyError("Pending action version is stale")
        raise InvalidStateTransitionError(
            PendingActionStatus(current.status),
            requested_status,
        )

    def _to_result(self, action: PendingAction) -> PendingActionResult:
        payload = self._validated_stored_payload(action)
        return PendingActionResult(
            id=action.id,
            business_id=action.business_id,
            action_type=PendingActionType(action.action_type),
            status=PendingActionStatus(action.status),
            payload_schema_version=action.payload_schema_version,
            payload=canonical_payload_dict(payload),
            payload_digest=action.payload_digest,
            confirmation_snapshot=action.confirmation_snapshot,
            expires_at=action.expires_at,
            version=action.version,
            committed_entity_type=action.committed_entity_type,
            committed_entity_id=action.committed_entity_id,
            error_code=action.commit_error_code,
            rejection_reason_code=action.rejection_reason_code,
            created_at=action.created_at,
            updated_at=action.updated_at,
            confirmed_at=action.confirmed_at,
        )
