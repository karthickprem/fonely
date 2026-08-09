"""Full semantic equivalence tests for committed notification event pairs."""

from contextlib import asynccontextmanager
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


def _legacy_events(
    service: NotificationService, snapshot: NotificationPairSnapshot
) -> tuple[MagicMock, MagicMock]:
    patient_values, owner_values = service._event_values(snapshot)
    patient_payload, owner_payload = service._legacy_payloads(snapshot)
    patient_values["payload"] = patient_payload
    owner_values["payload"] = owner_payload
    return _event(patient_values, 81), _event(owner_values, 82)


def _allow_nested(service: NotificationService) -> None:
    @asynccontextmanager
    async def nested():  # type: ignore[no-untyped-def]
        yield

    service._session.begin_nested = nested


def _verify_kwargs(snapshot: NotificationPairSnapshot) -> dict[str, object]:
    return {
        "business_id": snapshot.business_id,
        "keys": NotificationService._keys(snapshot),
        "operation": snapshot.operation,
        "appointment_id": snapshot.appointment_id,
        "patient_phone": snapshot.patient_phone,
        "patient_name": snapshot.patient_name,
        "service_name": snapshot.service_name,
        "resource_name": snapshot.resource_name,
        "business_timezone": snapshot.business_timezone,
        "start_at": snapshot.start_at,
        "old_start_at": snapshot.old_start_at,
        "new_start_at": snapshot.new_start_at,
        "price": snapshot.price,
        "reason": snapshot.reason,
        "pending_action_id": snapshot.pending_action_id,
    }


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
    _allow_nested(service)
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
@pytest.mark.parametrize("missing", ["patient", "owner"])
async def test_missing_pair_member_repairs_exactly_once(operation: str, missing: str) -> None:
    service = NotificationService(AsyncMock())
    _allow_nested(service)
    snapshot = _snapshot(operation)
    patient, owner = service._event_values(snapshot)
    patient_event = _event(patient, 71)
    owner_event = _event(owner, 72)
    inserted = _event(patient if missing == "patient" else owner, 73)
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = (
        [None, owner_event] if missing == "patient" else [patient_event, None]
    )
    service._repo.insert_event_idempotent.side_effect = [inserted]

    expected_ids = [73, 72] if missing == "patient" else [71, 73]
    assert (
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot)) == expected_ids
    )


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_pair_insert_failure_is_propagated_inside_savepoint(
    operation: str,
) -> None:
    events: list[str] = []
    session = AsyncMock()

    @asynccontextmanager
    async def nested():  # type: ignore[no-untyped-def]
        events.append("enter")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise

    session.begin_nested = nested
    service = NotificationService(session)
    service._repo = AsyncMock()
    service._repo.insert_event_idempotent.side_effect = [
        _event(service._event_values(_snapshot(operation))[0], 1),
        RuntimeError("second_insert_failed"),
    ]

    with pytest.raises(RuntimeError, match="second_insert_failed"):
        await service._create_or_verify_pair(_snapshot(operation))
    assert events == ["enter", "rollback"]


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_complete_legacy_pair_returns_durable_ids(operation: str) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient, owner = _legacy_events(service, snapshot)
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [patient, owner]

    assert await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot)) == [81, 82]
    service._repo.insert_event_idempotent.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
@pytest.mark.parametrize("missing", ["patient", "owner"])
async def test_missing_legacy_member_fails_before_insert(operation: str, missing: str) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient, owner = _legacy_events(service, snapshot)
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = (
        [None, owner] if missing == "patient" else [patient, None]
    )

    with pytest.raises(NotificationIdempotencyConflictError, match="cannot be reconstructed"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))
    service._repo.insert_event_idempotent.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_conflicting_complete_legacy_pair_fails_before_insert(
    operation: str,
) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient, owner = _legacy_events(service, snapshot)
    owner.payload = dict(owner.payload)
    owner.payload["patient_phone"] = "+918888888888"
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [patient, owner]

    with pytest.raises(NotificationIdempotencyConflictError, match="identity"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))
    service._repo.insert_event_idempotent.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_partially_versioned_member_fails_before_insert(operation: str) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient, owner = service._event_values(snapshot)
    patient = deepcopy(patient)
    patient_payload = patient["payload"]
    assert isinstance(patient_payload, dict)
    patient_payload.pop("equivalence_digest")
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [
        _event(patient, 81),
        _event(owner, 82),
    ]

    with pytest.raises(NotificationIdempotencyConflictError, match="partially versioned"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))
    service._repo.insert_event_idempotent.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_mixed_legacy_and_v1_pair_fails_before_insert(operation: str) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient, _ = _legacy_events(service, snapshot)
    _, owner_values = service._event_values(snapshot)
    owner = _event(owner_values, 82)
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [patient, owner]

    with pytest.raises(NotificationIdempotencyConflictError, match="mixes"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))
    service._repo.insert_event_idempotent.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_naive_time_evidence_fails_with_controlled_conflict(
    operation: str,
) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient_values, owner_values = service._event_values(snapshot)
    patient_payload = dict(patient_values["payload"])
    snapshot_payload = dict(patient_payload["equivalence_snapshot"])
    time_field = "start_at" if operation != "reschedule" else "old_start_at"
    snapshot_payload[time_field] = "2026-08-12T10:00:00"
    patient_payload["equivalence_snapshot"] = snapshot_payload
    patient_values["payload"] = patient_payload
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [
        _event(patient_values, 81),
        _event(owner_values, 82),
    ]

    with pytest.raises(NotificationIdempotencyConflictError, match="snapshot is invalid"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_missing_both_pair_members_fails_closed(operation: str) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [None, None]

    with pytest.raises(NotificationIdempotencyConflictError, match="missing"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))
