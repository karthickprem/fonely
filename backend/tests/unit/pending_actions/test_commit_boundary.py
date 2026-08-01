"""Trusted internal commit boundary tests."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from fonely.domain.pending_actions.commands import CommitResultContext
from fonely.domain.pending_actions.errors import TrustedCommitContextError
from fonely.models.enums import PendingActionType
from fonely.models.schema import PendingAction
from fonely.services.pending_actions import PendingActionService


def action(action_type: PendingActionType) -> PendingAction:
    proposed_payload: dict[str, object] = {}
    digest = "0" * 64
    if action_type == PendingActionType.APPOINTMENT:
        from fonely.domain.pending_actions.payloads import validate_payload
        from fonely.domain.pending_actions.snapshots import canonical_payload_dict, payload_digest

        proposed_payload = {
            "schema_version": 1,
            "action_type": "appointment",
            "data": {
                "operation": "create",
                "facts": {
                    "service_id": 1,
                    "service_name": "Haircut",
                    "resource_id": 1,
                    "resource_name": "Priya",
                    "start_at": "2026-08-02T10:00:00Z",
                    "end_at": "2026-08-02T10:30:00Z",
                    "effective_start_at": "2026-08-02T10:00:00Z",
                    "effective_end_at": "2026-08-02T10:30:00Z",
                    "duration_minutes": 30,
                    "business_timezone": "Asia/Kolkata",
                },
                "customer_phone": "+919123456789",
            },
        }
        validated = validate_payload(action_type, 1, proposed_payload)
        proposed_payload = canonical_payload_dict(validated)
        digest = payload_digest(validated)
    return PendingAction(
        id=10,
        business_id=1,
        action_type=action_type.value,
        payload_schema_version=1,
        proposed_payload=proposed_payload,
        payload_digest=digest,
        expires_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        idempotency_key="key",
        version=1,
    )


def context(engine: str) -> CommitResultContext:
    return CommitResultContext(
        business_id=1,
        pending_action_id=10,
        expected_version=1,
        engine=engine,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("action_type", "engine", "entity_type"),
    [
        (PendingActionType.ORDER, "order_engine", "order"),
        (PendingActionType.APPOINTMENT, "appointment_engine", "appointment"),
        (
            PendingActionType.OWNER_STOCK_UPDATE,
            "inventory_engine",
            "inventory_update",
        ),
    ],
)
def test_action_type_maps_to_trusted_engine_and_entity(
    action_type: PendingActionType,
    engine: str,
    entity_type: str,
) -> None:
    service = PendingActionService(AsyncMock())
    actual_type, _ = service._assert_trusted_engine(action(action_type), context(engine))
    assert actual_type == entity_type


def test_wrong_engine_rejected() -> None:
    service = PendingActionService(AsyncMock())
    with pytest.raises(TrustedCommitContextError, match="engine"):
        service._assert_trusted_engine(
            action(PendingActionType.ORDER),
            context("appointment_engine"),
        )


def test_unimplemented_action_engine_rejected() -> None:
    service = PendingActionService(AsyncMock())
    with pytest.raises(TrustedCommitContextError, match="No commit engine"):
        service._assert_trusted_engine(
            action(PendingActionType.OWNER_PRICE_UPDATE),
            context("inventory_engine"),
        )


async def test_correct_entity_business_and_pending_action_succeeds() -> None:
    session = AsyncMock()
    session.scalar.return_value = 99
    service = PendingActionService(session)
    _, entity_model = service._assert_trusted_engine(
        action(PendingActionType.ORDER),
        context("order_engine"),
    )
    await service._require_committed_entity(
        entity_model,
        business_id=1,
        entity_id=99,
        pending_action_id=10,
    )


@pytest.mark.parametrize(
    ("business_id", "entity_id", "pending_action_id"),
    [
        (1, 99, 11),  # same business/entity, wrong pending action
        (2, 99, 10),  # cross-business entity
        (1, 999, 10),  # nonexistent entity
    ],
)
async def test_unlinked_committed_entity_rejected(
    business_id: int,
    entity_id: int,
    pending_action_id: int,
) -> None:
    session = AsyncMock()
    session.scalar.return_value = None
    service = PendingActionService(session)
    _, entity_model = service._assert_trusted_engine(
        action(PendingActionType.ORDER),
        context("order_engine"),
    )
    with pytest.raises(TrustedCommitContextError, match="not found"):
        await service._require_committed_entity(
            entity_model,
            business_id=business_id,
            entity_id=entity_id,
            pending_action_id=pending_action_id,
        )
    statement = session.scalar.call_args.args[0]
    compiled = str(statement)
    assert "orders.business_id" in compiled
    assert "orders.id" in compiled
    assert "orders.pending_action_id" in compiled
