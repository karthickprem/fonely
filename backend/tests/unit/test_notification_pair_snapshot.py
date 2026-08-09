"""Full semantic equivalence tests for committed notification event pairs."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from fonely.services.notifications import (
    NotificationIdempotencyConflictError,
    NotificationPairSnapshot,
    NotificationService,
)

NOW = datetime(2026, 8, 12, 10, tzinfo=UTC)


def _snapshot(operation: str) -> NotificationPairSnapshot:
    values = {
        "schema_version": 1,
        "operation": operation,
        "business_id": 1,
        "appointment_id": 9,
        "pending_action_id": 44 if operation == "reschedule" else None,
        "clinic_name": "Smile Dental",
        "patient_phone": "+919123456789",
        "patient_name": "Patient",
        "owner_phone": "+919000000001",
        "phone_number_id": "phone-1",
        "service_name": "Consultation",
        "resource_name": "Dr. Priya",
        "business_timezone": "Asia/Kolkata",
        "start_at": NOW if operation != "reschedule" else None,
        "old_start_at": NOW if operation == "reschedule" else None,
        "new_start_at": NOW + timedelta(hours=2) if operation == "reschedule" else None,
        "price": "300" if operation == "create" else None,
        "reason": "Requested" if operation == "cancel" else None,
    }
    return NotificationPairSnapshot.model_validate(values)


def _event(values: dict[str, object], event_id: int) -> MagicMock:
    return MagicMock(id=event_id, **values)


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("business_id",), 2),
        (("event_type",), "wrong_event"),
        (("entity_type",), "wrong_entity"),
        (("entity_id",), 10),
        (("recipient_type",), "wrong_recipient"),
        (("recipient_phone",), "+919999999999"),
        (("recipient_name",), "Wrong Name"),
        (("channel",), "internal"),
        (("status",), "invalid"),
        (("payload", "schema_version"), 2),
        (("payload", "template_type"), "wrong_template"),
        (("payload", "clinic_name"), "Wrong Clinic"),
        (("payload", "service"), "Wrong Service"),
        (("payload", "doctor"), "Wrong Doctor"),
        (("payload", "appointment_id"), 10),
        (("payload", "phone_number_id"), "wrong-phone"),
        (("payload", "equivalence_digest"), "0" * 64),
    ],
)
def test_each_persisted_semantic_mutation_is_non_equivalent(
    operation: str, path: tuple[str, ...], value: object
) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    expected, _ = service._event_values(snapshot)
    persisted = deepcopy(expected)
    target: dict[str, object] = persisted
    for segment in path[:-1]:
        nested = target[segment]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    assert service._event_equivalent(_event(persisted, 1), expected) is False


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("recipient_phone",), "+919999999999"),
        (("recipient_name",), "Unexpected Owner Name"),
        (("payload", "patient_name"), "Wrong Patient"),
        (("payload", "patient_phone"), "+918888888888"),
    ],
)
def test_owner_event_semantic_mutation_is_non_equivalent(
    operation: str, path: tuple[str, ...], value: object
) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    _, expected = service._event_values(snapshot)
    persisted = deepcopy(expected)
    target: dict[str, object] = persisted
    for segment in path[:-1]:
        nested = target[segment]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    assert service._event_equivalent(_event(persisted, 2), expected) is False


@pytest.mark.parametrize(
    ("operation", "field", "value"),
    [
        ("create", "price", "₹999"),
        ("cancel", "reason", "Wrong reason"),
        ("reschedule", "old_time", "1:00 AM"),
        ("reschedule", "new_time", "2:00 AM"),
    ],
)
def test_operation_specific_payload_mutation_is_non_equivalent(
    operation: str, field: str, value: object
) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    expected, _ = service._event_values(snapshot)
    persisted = deepcopy(expected)
    payload = persisted["payload"]
    assert isinstance(payload, dict)
    payload[field] = value
    assert service._event_equivalent(_event(persisted, 3), expected) is False


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_exact_pair_returns_existing_ids_without_current_configuration(
    operation: str,
) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient, owner = service._event_values(snapshot)
    patient_event = _event(patient, 71)
    owner_event = _event(owner, 72)
    service._repo = AsyncMock()
    service._repo.insert_event_idempotent.side_effect = [None, None]
    service._repo.get_event_by_idempotency_key.side_effect = [patient_event, owner_event]

    assert await service._create_or_verify_pair(snapshot) == [71, 72]
    service._repo.get_event_by_global_idempotency_key.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_missing_pair_member_repairs_exactly_once(operation: str) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient, owner = service._event_values(snapshot)
    patient_event = _event(patient, 71)
    inserted_owner = _event(owner, 73)
    keys = service._keys(snapshot)
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [patient_event, None]
    loaded = await service._load_committed_snapshot(business_id=1, keys=keys)
    assert loaded == snapshot

    service._repo.insert_event_idempotent.side_effect = [None, inserted_owner]
    service._repo.get_event_by_idempotency_key.side_effect = [patient_event]
    assert await service._create_or_verify_pair(loaded) == [71, 73]


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_missing_both_pair_members_fails_closed(operation: str) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [None, None]

    with pytest.raises(NotificationIdempotencyConflictError, match="missing"):
        await service._load_committed_snapshot(business_id=1, keys=service._keys(snapshot))
