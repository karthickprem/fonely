"""Appointment-specific PendingAction validation and completion contracts."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from unittest.mock import AsyncMock, patch

import pytest

from fonely.domain.pending_actions.commands import (
    ActorContext,
    BeginCommitCommand,
    CancelPendingActionCommand,
    CommitResultContext,
    CompleteCommitCommand,
    CreatePendingActionCommand,
    ExpirePendingActionCommand,
    FailCommitCommand,
    MarkAwaitingConfirmationCommand,
    RejectPendingActionCommand,
    RevisePendingActionCommand,
)
from fonely.domain.pending_actions.errors import (
    CommitEntityConflictError,
    PendingActionIdempotencyConflictError,
    TrustedCommitContextError,
)
from fonely.domain.pending_actions.payloads import PendingAppointmentEnvelope, validate_payload
from fonely.domain.pending_actions.snapshots import (
    canonical_payload_dict,
    confirmation_snapshot,
    payload_digest,
)
from fonely.models.enums import CallerRole, Channel, PendingActionStatus, PendingActionType
from fonely.models.schema import Appointment, AppointmentCommit, PendingAction
from fonely.services.pending_actions import PendingActionService

NOW = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)


def actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
        session_id="session-1",
    )


def facts(
    *, resource_name: str = "Priya", hour: int = 10, timezone: str = "Asia/Kolkata"
) -> dict[str, object]:
    start = datetime(2026, 8, 2, hour, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    return {
        "service_id": 4,
        "service_name": "Haircut",
        "resource_id": 7,
        "resource_name": resource_name,
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "effective_start_at": start.isoformat(),
        "effective_end_at": end.isoformat(),
        "duration_minutes": 30,
        "buffer_before_minutes": 0,
        "buffer_after_minutes": 0,
        "price": "500.00",
        "business_timezone": timezone,
    }


def appointment_payload(operation: str = "create") -> dict[str, object]:
    if operation == "create":
        data: dict[str, object] = {
            "operation": "create",
            "facts": facts(),
            "customer_name": "Asha",
            "customer_phone": "+919123456789",
            "reason": None,
            "call_id": None,
        }
    elif operation == "cancel":
        data = {
            "operation": "cancel",
            "target_appointment_id": 44,
            "target_expected_version": 3,
            "current_facts": facts(),
            "reason_code": "customer_request",
        }
    else:
        data = {
            "operation": "reschedule",
            "target_appointment_id": 44,
            "target_expected_version": 3,
            "old_facts": facts(),
            "new_facts": facts(hour=11),
        }
    return {"schema_version": 1, "action_type": "appointment", "data": data}


class ValidationFake:
    def __init__(self, authoritative: PendingAppointmentEnvelope | None = None) -> None:
        self.authoritative = authoritative
        self.actor_calls = 0
        self.stored_calls = 0
        self.completion_calls = 0

    async def validate_for_actor(
        self, actor_context: ActorContext, payload: PendingAppointmentEnvelope
    ) -> PendingAppointmentEnvelope:
        assert actor_context.business_id == 1
        self.actor_calls += 1
        return self.authoritative or payload

    async def validate_stored(
        self, business_id: int, payload: PendingAppointmentEnvelope
    ) -> PendingAppointmentEnvelope:
        assert business_id == 1
        self.stored_calls += 1
        return self.authoritative or payload

    async def validate_idempotent_retry(
        self,
        actor_context: ActorContext,
        proposed: PendingAppointmentEnvelope,
        stored: PendingAppointmentEnvelope,
    ) -> None:
        assert actor_context.business_id == 1
        if proposed != stored:
            raise TrustedCommitContextError("Retry payload differs")

    async def validate_completion_evidence(
        self,
        business_id: int,
        payload: PendingAppointmentEnvelope,
        committed_entity_type: str,
        committed_entity_id: int,
    ) -> None:
        assert business_id == 1
        assert committed_entity_id > 0
        self.completion_calls += 1
        expected = "appointment" if payload.data.operation == "create" else "appointment_commit"
        if committed_entity_type != expected:
            raise TrustedCommitContextError("Wrong completion evidence")


class RepoFake:
    def __init__(self, current: PendingAction | None = None) -> None:
        self.current = current

    async def get_by_idempotency_key(self, business_id: int, key: str) -> PendingAction | None:
        return self.current

    async def insert_idempotent(self, values: dict[str, object]) -> PendingAction:
        self.current = PendingAction(id=11, created_at=NOW, updated_at=NOW, **values)
        return self.current

    async def get_by_id(self, business_id: int, action_id: int) -> PendingAction | None:
        return self.current if self.current and self.current.id == action_id else None

    async def conditional_update(self, **kwargs: object) -> PendingAction | None:
        assert self.current is not None
        values = kwargs["values"]
        assert isinstance(values, dict)
        for key, value in values.items():
            setattr(self.current, key, value)
        self.current.version += 1
        return self.current


def stored_action(operation: str, status: PendingActionStatus) -> PendingAction:
    envelope = validate_payload(PendingActionType.APPOINTMENT, 1, appointment_payload(operation))
    return PendingAction(
        id=11,
        business_id=1,
        session_id="session-1",
        action_type="appointment",
        payload_schema_version=1,
        proposed_payload=canonical_payload_dict(envelope),
        payload_digest=payload_digest(envelope),
        confirmation_snapshot=(
            confirmation_snapshot(envelope)
            if status != PendingActionStatus.COLLECTING_DETAILS
            else None
        ),
        status=status.value,
        expires_at=NOW + timedelta(minutes=15),
        idempotency_key="key",
        initiated_by="+919123456789",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture(autouse=True)
def common_dependencies() -> object:
    with (
        patch.object(PendingActionService, "_require_business", new=AsyncMock(return_value=None)),
        patch.object(
            PendingActionService,
            "_validate_new_payload_products",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            PendingActionService,
            "_validate_stored_payload_ownership",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "fonely.services.pending_actions.require_action_permission",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "fonely.services.pending_actions.require_existing_action_permission",
            new=AsyncMock(return_value=None),
        ),
        patch("fonely.services.pending_actions.utcnow", return_value=NOW),
    ):
        yield


async def test_validation_port_output_is_strictly_revalidated() -> None:
    valid = PendingAppointmentEnvelope.model_validate(appointment_payload())
    malformed = PendingAppointmentEnvelope.model_construct(
        schema_version=1,
        action_type=PendingActionType.APPOINTMENT,
        data={"operation": "create", "facts": {"duration_minutes": -1}},
    )
    validation = ValidationFake(malformed)
    service = PendingActionService(AsyncMock(), appointment_validation=validation)
    service._repo = RepoFake()  # type: ignore[assignment]

    with pytest.raises(TrustedCommitContextError, match="invalid payload"):
        await service.create(
            CreatePendingActionCommand(
                actor=actor(),
                action_type=PendingActionType.APPOINTMENT,
                payload=appointment_payload(),
                expires_at=NOW + timedelta(minutes=15),
                idempotency_key="key",
            )
        )

    reconstructed = service._revalidate_appointment_envelope(valid)
    assert reconstructed == valid
    assert payload_digest(reconstructed) == payload_digest(valid)


async def test_create_persists_authoritative_payload_from_injected_port() -> None:
    raw = appointment_payload()
    raw_data = raw["data"]
    assert isinstance(raw_data, dict)
    raw_data["facts"] = facts(timezone="America/New_York")
    authoritative_raw = appointment_payload()
    data = authoritative_raw["data"]
    assert isinstance(data, dict)
    authoritative_facts = data["facts"]
    assert isinstance(authoritative_facts, dict)
    authoritative_facts["resource_name"] = "Authoritative Priya"
    authoritative = PendingAppointmentEnvelope.model_validate(authoritative_raw)
    validation = ValidationFake(authoritative)
    service = PendingActionService(AsyncMock(), appointment_validation=validation)
    repo = RepoFake()
    service._repo = repo  # type: ignore[assignment]

    result = await service.create(
        CreatePendingActionCommand(
            actor=actor(),
            action_type=PendingActionType.APPOINTMENT,
            payload=raw,
            expires_at=NOW + timedelta(minutes=15),
            idempotency_key="key",
        )
    )

    assert result.payload["data"]["facts"]["resource_name"] == "Authoritative Priya"
    assert result.payload["data"]["facts"]["business_timezone"] == "Asia/Kolkata"
    assert validation.actor_calls == 1


@pytest.mark.parametrize(
    ("operation", "fact_keys"),
    [
        ("cancel", ("current_facts",)),
        ("reschedule", ("old_facts", "new_facts")),
    ],
)
async def test_mutation_create_persists_authoritative_business_timezone(
    operation: str, fact_keys: tuple[str, ...]
) -> None:
    raw = appointment_payload(operation)
    raw_data = raw["data"]
    assert isinstance(raw_data, dict)
    for fact_key in fact_keys:
        submitted_facts = raw_data[fact_key]
        assert isinstance(submitted_facts, dict)
        submitted_facts["business_timezone"] = "America/New_York"

    authoritative = PendingAppointmentEnvelope.model_validate(appointment_payload(operation))
    service = PendingActionService(
        AsyncMock(), appointment_validation=ValidationFake(authoritative)
    )
    service._repo = RepoFake()  # type: ignore[assignment]

    result = await service.create(
        CreatePendingActionCommand(
            actor=actor(),
            action_type=PendingActionType.APPOINTMENT,
            payload=raw,
            expires_at=NOW + timedelta(minutes=15),
            idempotency_key=f"{operation}-key",
        )
    )

    result_data = result.payload["data"]
    assert isinstance(result_data, dict)
    for fact_key in fact_keys:
        result_facts = result_data[fact_key]
        assert isinstance(result_facts, dict)
        assert result_facts["business_timezone"] == "Asia/Kolkata"


async def test_appointment_create_fails_closed_without_validation_port() -> None:
    service = PendingActionService(AsyncMock())
    service._repo = RepoFake()  # type: ignore[assignment]
    with pytest.raises(TrustedCommitContextError, match="not configured"):
        await service.create(
            CreatePendingActionCommand(
                actor=actor(),
                action_type=PendingActionType.APPOINTMENT,
                payload=appointment_payload(),
                expires_at=NOW + timedelta(minutes=15),
                idempotency_key="key",
            )
        )


@pytest.mark.parametrize("operation", ["cancel", "reschedule"])
async def test_mutation_snapshot_is_bound_to_target(operation: str) -> None:
    current = stored_action(operation, PendingActionStatus.COLLECTING_DETAILS)
    service = PendingActionService(AsyncMock(), appointment_validation=ValidationFake())
    service._repo = RepoFake(current)  # type: ignore[assignment]

    result = await service.mark_awaiting_confirmation(
        MarkAwaitingConfirmationCommand(actor=actor(), action_id=11, expected_version=1)
    )

    assert result.confirmation_snapshot is not None
    snapshot = json.loads(result.confirmation_snapshot)
    assert snapshot["facts"]["target_appointment_id"] == 44
    assert "target_expected_version" not in snapshot["facts"]
    if operation == "reschedule":
        assert snapshot["facts"]["old_facts"]["start_at"] != snapshot["facts"]["start_at"]


@pytest.mark.parametrize(
    ("operation", "entity_type", "entity_model"),
    [
        ("create", "appointment", Appointment),
        ("cancel", "appointment_commit", AppointmentCommit),
        ("reschedule", "appointment_commit", AppointmentCommit),
    ],
)
def test_completion_evidence_is_operation_aware(
    operation: str, entity_type: str, entity_model: type[object]
) -> None:
    current = stored_action(operation, PendingActionStatus.COMMITTING)
    service = PendingActionService(AsyncMock(), appointment_validation=ValidationFake())
    actual_type, actual_model = service._assert_trusted_engine(
        current,
        CommitResultContext(
            business_id=1,
            pending_action_id=11,
            expected_version=1,
            engine="appointment_engine",
        ),
    )
    assert actual_type == entity_type
    assert actual_model is entity_model


async def test_revise_replaces_proposal_after_authoritative_fact_drift() -> None:
    current = stored_action("create", PendingActionStatus.AWAITING_CONFIRMATION)
    authoritative_raw = appointment_payload()
    authoritative_data = authoritative_raw["data"]
    assert isinstance(authoritative_data, dict)
    authoritative_facts = authoritative_data["facts"]
    assert isinstance(authoritative_facts, dict)
    authoritative_facts["resource_name"] = "Replacement Priya"
    authoritative = PendingAppointmentEnvelope.model_validate(authoritative_raw)
    validation = ValidationFake(authoritative)
    service = PendingActionService(AsyncMock(), appointment_validation=validation)
    service._repo = RepoFake(current)  # type: ignore[assignment]

    result = await service.revise(
        RevisePendingActionCommand(
            actor=actor(),
            action_id=11,
            expected_version=1,
            payload=appointment_payload(),
        )
    )

    assert result.status == PendingActionStatus.COLLECTING_DETAILS
    assert result.payload["data"]["facts"]["resource_name"] == "Replacement Priya"
    assert result.confirmation_snapshot is None
    assert validation.stored_calls == 0
    assert validation.actor_calls == 1


@pytest.mark.parametrize("transition", ["cancel", "reject", "expire", "fail_commit"])
async def test_cleanup_transition_survives_authoritative_fact_drift(transition: str) -> None:
    source_status = (
        PendingActionStatus.COMMITTING
        if transition == "fail_commit"
        else PendingActionStatus.AWAITING_CONFIRMATION
    )
    current = stored_action("create", source_status)
    if transition == "expire":
        current.expires_at = NOW
    authoritative_raw = appointment_payload()
    authoritative_data = authoritative_raw["data"]
    assert isinstance(authoritative_data, dict)
    authoritative_facts = authoritative_data["facts"]
    assert isinstance(authoritative_facts, dict)
    authoritative_facts["resource_name"] = "Changed Priya"
    authoritative = PendingAppointmentEnvelope.model_validate(authoritative_raw)
    validation = ValidationFake(authoritative)
    service = PendingActionService(AsyncMock(), appointment_validation=validation)
    service._repo = RepoFake(current)  # type: ignore[assignment]

    if transition == "cancel":
        result = await service.cancel(
            CancelPendingActionCommand(actor=actor(), action_id=11, expected_version=1)
        )
        expected_status = PendingActionStatus.CANCELLED
    elif transition == "reject":
        result = await service.reject(
            RejectPendingActionCommand(
                actor=actor(),
                action_id=11,
                expected_version=1,
                reason_code="customer_declined",
            )
        )
        expected_status = PendingActionStatus.REJECTED
    elif transition == "expire":
        result = await service.expire(
            ExpirePendingActionCommand(
                business_id=1,
                action_id=11,
                expected_version=1,
                now=NOW,
            )
        )
        expected_status = PendingActionStatus.EXPIRED
    else:
        result = await service.fail_commit(
            FailCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=11,
                    expected_version=1,
                    engine="appointment_engine",
                ),
                error_code="resource_unavailable",
                retryable=True,
            )
        )
        expected_status = PendingActionStatus.AWAITING_CONFIRMATION

    assert result.status == expected_status
    assert validation.stored_calls == 0


async def test_begin_commit_revalidates_stored_appointment() -> None:
    current = stored_action("create", PendingActionStatus.AWAITING_CONFIRMATION)
    validation = ValidationFake()
    service = PendingActionService(AsyncMock(), appointment_validation=validation)
    service._repo = RepoFake(current)  # type: ignore[assignment]
    await service.begin_commit(
        BeginCommitCommand(
            context=CommitResultContext(
                business_id=1,
                pending_action_id=11,
                expected_version=1,
                engine="appointment_engine",
            )
        )
    )
    assert validation.stored_calls >= 1


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_stored_revalidation_rejects_changed_authoritative_timezone(
    operation: str,
) -> None:
    current = stored_action(operation, PendingActionStatus.AWAITING_CONFIRMATION)
    authoritative_raw = appointment_payload(operation)
    authoritative_data = authoritative_raw["data"]
    assert isinstance(authoritative_data, dict)
    fact_keys = {
        "create": ("facts",),
        "cancel": ("current_facts",),
        "reschedule": ("old_facts", "new_facts"),
    }[operation]
    for fact_key in fact_keys:
        authoritative_facts = authoritative_data[fact_key]
        assert isinstance(authoritative_facts, dict)
        authoritative_facts["business_timezone"] = "America/New_York"
    authoritative = PendingAppointmentEnvelope.model_validate(authoritative_raw)
    service = PendingActionService(
        AsyncMock(), appointment_validation=ValidationFake(authoritative)
    )
    service._repo = RepoFake(current)  # type: ignore[assignment]

    with pytest.raises(PendingActionIdempotencyConflictError, match="facts changed"):
        await service.begin_commit(
            BeginCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=11,
                    expected_version=1,
                    engine="appointment_engine",
                )
            )
        )


@pytest.mark.parametrize(
    ("operation", "entity_type"),
    [
        ("create", "appointment"),
        ("cancel", "appointment_commit"),
        ("reschedule", "appointment_commit"),
    ],
)
async def test_initial_complete_commit_validates_appointment_evidence(
    operation: str, entity_type: Literal["appointment", "appointment_commit"]
) -> None:
    current = stored_action(operation, PendingActionStatus.COMMITTING)
    validation = ValidationFake()
    service = PendingActionService(AsyncMock(), appointment_validation=validation)
    service._repo = RepoFake(current)  # type: ignore[assignment]

    with patch.object(service, "_require_committed_entity", new=AsyncMock(return_value=None)):
        result = await service.complete_commit(
            CompleteCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=11,
                    expected_version=1,
                    engine="appointment_engine",
                ),
                committed_entity_type=entity_type,
                committed_entity_id=99,
            )
        )

    assert result.status == PendingActionStatus.CONFIRMED
    assert validation.completion_calls == 1


async def test_confirmed_appointment_retry_skips_mutable_completion_evidence() -> None:
    current = stored_action("create", PendingActionStatus.CONFIRMED)
    current.committed_entity_type = "appointment"
    current.committed_entity_id = 99
    validation = ValidationFake()
    service = PendingActionService(AsyncMock(), appointment_validation=validation)
    service._repo = RepoFake(current)  # type: ignore[assignment]
    entity_lookup = AsyncMock(side_effect=AssertionError("confirmed retry revalidated entity"))
    evidence = AsyncMock(side_effect=AssertionError("confirmed retry revalidated evidence"))

    with (
        patch.object(service, "_require_committed_entity", new=entity_lookup),
        patch.object(validation, "validate_completion_evidence", new=evidence),
    ):
        result = await service.complete_commit(
            CompleteCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=11,
                    expected_version=1,
                    engine="appointment_engine",
                ),
                committed_entity_type="appointment",
                committed_entity_id=99,
            )
        )

    assert result.status == PendingActionStatus.CONFIRMED
    assert result.committed_entity_id == 99
    entity_lookup.assert_not_awaited()
    evidence.assert_not_awaited()


async def test_confirmed_appointment_conflict_precedes_mutable_evidence() -> None:
    current = stored_action("create", PendingActionStatus.CONFIRMED)
    current.committed_entity_type = "appointment"
    current.committed_entity_id = 99
    validation = ValidationFake()
    service = PendingActionService(AsyncMock(), appointment_validation=validation)
    service._repo = RepoFake(current)  # type: ignore[assignment]
    entity_lookup = AsyncMock(side_effect=AssertionError("confirmed retry revalidated entity"))
    evidence = AsyncMock(side_effect=AssertionError("confirmed retry revalidated evidence"))

    with (
        patch.object(service, "_require_committed_entity", new=entity_lookup),
        patch.object(validation, "validate_completion_evidence", new=evidence),
        pytest.raises(CommitEntityConflictError),
    ):
        await service.complete_commit(
            CompleteCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=11,
                    expected_version=1,
                    engine="appointment_engine",
                ),
                committed_entity_type="appointment",
                committed_entity_id=100,
            )
        )

    entity_lookup.assert_not_awaited()
    evidence.assert_not_awaited()


async def test_complete_mutation_rejects_appointment_evidence() -> None:
    current = stored_action("cancel", PendingActionStatus.COMMITTING)
    service = PendingActionService(AsyncMock(), appointment_validation=ValidationFake())
    service._repo = RepoFake(current)  # type: ignore[assignment]
    with pytest.raises(TrustedCommitContextError, match="entity type"):
        await service.complete_commit(
            CompleteCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=11,
                    expected_version=1,
                    engine="appointment_engine",
                ),
                committed_entity_type="appointment",
                committed_entity_id=99,
            )
        )
