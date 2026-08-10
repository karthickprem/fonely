"""Full semantic equivalence tests for committed notification event pairs.

Tests exact-v1 evidence policy: only versioned v1+ payloads authorize
automated replay or repair. All non-exact historical formats produce
legacy_unverifiable and require manual reconciliation.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from fonely.core.metrics import metrics
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


# ── Per-field mutation tests ────────────────────────────────────────


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


# ── Exact v1 pair tests ─────────────────────────────────────────────


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_exact_pair_returns_existing_ids(operation: str) -> None:
    service = NotificationService(AsyncMock())
    _allow_nested(service)
    snapshot = _snapshot(operation)
    patient, owner = service._event_values(snapshot)
    patient_event = _event(patient, 71)
    owner_event = _event(owner, 72)
    service._repo = AsyncMock()
    service._repo.insert_event_idempotent.side_effect = [None, None]
    service._repo.get_event_by_idempotency_key.side_effect = [
        patient_event,
        owner_event,
    ]

    assert await service._create_or_verify_pair(snapshot) == [71, 72]
    service._repo.get_event_by_global_idempotency_key.assert_not_awaited()


# ── Missing v1 member repair tests ──────────────────────────────────


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
@pytest.mark.parametrize("missing", ["patient", "owner"])
async def test_missing_v1_member_repairs_exactly_once(operation: str, missing: str) -> None:
    metrics.reset()
    service = NotificationService(AsyncMock())
    _allow_nested(service)
    snapshot = _snapshot(operation)
    patient, owner = service._event_values(snapshot)
    patient_event = _event(patient, 71)
    owner_event = _event(owner, 72)
    inserted = _event(patient if missing == "patient" else owner, 73)
    service._repo = AsyncMock()
    # Called 4 times: 2 initial reads + 2 locked re-reads inside savepoint
    if missing == "patient":
        service._repo.get_event_by_idempotency_key.side_effect = [
            None,
            owner_event,  # initial: patient missing, owner exists
            None,
            owner_event,  # locked re-read: same state
        ]
    else:
        service._repo.get_event_by_idempotency_key.side_effect = [
            patient_event,
            None,  # initial: patient exists, owner missing
            patient_event,
            None,  # locked re-read: same state
        ]
    service._repo.insert_event_idempotent.side_effect = [inserted]

    expected_ids = [73, 72] if missing == "patient" else [71, 73]
    assert (
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot)) == expected_ids
    )

    assert (
        metrics.counter_value(
            "notification_reconciliation_total",
            {"operation": operation, "format": "v1", "outcome": "exact_repaired"},
        )
        == 1
    )


# ── Legacy fail-closed tests ────────────────────────────────────────


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_complete_legacy_pair_fails_closed(operation: str) -> None:
    """Even a complete legacy pair with matching fields is legacy_unverifiable."""
    metrics.reset()
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient_vals, owner_vals = service._event_values(snapshot)
    patient_payload = deepcopy(patient_vals["payload"])
    owner_payload = deepcopy(owner_vals["payload"])
    assert isinstance(patient_payload, dict)
    assert isinstance(owner_payload, dict)
    patient_payload.pop("equivalence_snapshot")
    patient_payload.pop("equivalence_digest")
    patient_payload.pop("schema_version")
    patient_payload.pop("template_type")
    owner_payload.pop("equivalence_snapshot")
    owner_payload.pop("equivalence_digest")
    owner_payload.pop("schema_version")
    owner_payload.pop("template_type")
    patient_vals["payload"] = patient_payload
    owner_vals["payload"] = owner_payload
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [
        _event(patient_vals, 81),
        _event(owner_vals, 82),
    ]

    with pytest.raises(NotificationIdempotencyConflictError, match="legacy_unverifiable"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))

    assert (
        metrics.counter_value(
            "notification_reconciliation_total",
            {"operation": operation, "format": "legacy", "outcome": "legacy_unverifiable"},
        )
        == 1
    )


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
@pytest.mark.parametrize("missing", ["patient", "owner"])
async def test_legacy_one_missing_fails_closed(operation: str, missing: str) -> None:
    metrics.reset()
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient_vals, owner_vals = service._event_values(snapshot)
    surviving_vals = owner_vals if missing == "patient" else patient_vals
    payload = deepcopy(surviving_vals["payload"])
    assert isinstance(payload, dict)
    payload.pop("equivalence_snapshot")
    payload.pop("equivalence_digest")
    payload.pop("schema_version")
    payload.pop("template_type")
    surviving_vals["payload"] = payload
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = (
        [None, _event(surviving_vals, 82)]
        if missing == "patient"
        else [_event(surviving_vals, 81), None]
    )

    with pytest.raises(NotificationIdempotencyConflictError, match="legacy_unverifiable"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))
    service._repo.insert_event_idempotent.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_mixed_legacy_and_v1_pair_fails_closed(operation: str) -> None:
    metrics.reset()
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient_vals, owner_vals = service._event_values(snapshot)
    patient_payload = deepcopy(patient_vals["payload"])
    assert isinstance(patient_payload, dict)
    patient_payload.pop("equivalence_snapshot")
    patient_payload.pop("equivalence_digest")
    patient_payload.pop("schema_version")
    patient_payload.pop("template_type")
    patient_vals["payload"] = patient_payload
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [
        _event(patient_vals, 81),
        _event(owner_vals, 82),
    ]

    with pytest.raises(NotificationIdempotencyConflictError, match="legacy_unverifiable"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))
    service._repo.insert_event_idempotent.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_partially_versioned_member_fails_closed(operation: str) -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    patient_vals, owner_vals = service._event_values(snapshot)
    patient_payload = deepcopy(patient_vals["payload"])
    assert isinstance(patient_payload, dict)
    patient_payload.pop("equivalence_digest")
    patient_vals["payload"] = patient_payload
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [
        _event(patient_vals, 81),
        _event(owner_vals, 82),
    ]

    with pytest.raises(NotificationIdempotencyConflictError, match="partially versioned"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))
    service._repo.insert_event_idempotent.assert_not_awaited()


# ── Both absent tests ───────────────────────────────────────────────


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_both_absent_fails_closed(operation: str) -> None:
    metrics.reset()
    service = NotificationService(AsyncMock())
    snapshot = _snapshot(operation)
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [None, None]

    with pytest.raises(NotificationIdempotencyConflictError, match="missing"):
        await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))

    assert (
        metrics.counter_value(
            "notification_reconciliation_total",
            {"operation": operation, "format": "none", "outcome": "missing_evidence"},
        )
        == 1
    )


# ── Savepoint rollback tests ───────────────────────────────────────


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_pair_insert_failure_propagates_inside_savepoint(
    operation: str,
) -> None:
    events: list[str] = []
    session = AsyncMock()

    @asynccontextmanager
    async def _tracking_nested():  # type: ignore[no-untyped-def]
        events.append("enter_nested")
        try:
            yield
        except Exception:
            events.append("rollback_nested")
            raise

    session.begin_nested = _tracking_nested
    service = NotificationService(session)
    snapshot = _snapshot(operation)
    service._repo = AsyncMock()
    service._repo.insert_event_idempotent.side_effect = [
        MagicMock(id=1),
        RuntimeError("second_insert_failed"),
    ]

    with pytest.raises(RuntimeError, match="second_insert_failed"):
        await service._create_or_verify_pair(snapshot)

    assert "enter_nested" in events
    assert "rollback_nested" in events


# ── Naive timestamp test ────────────────────────────────────────────


def test_naive_datetime_fails_validation() -> None:
    from datetime import datetime as dt

    with pytest.raises(ValueError):
        NotificationPairSnapshot(
            schema_version=1,
            operation="create",
            business_id=1,
            appointment_id=1,
            clinic_name="Test",
            patient_phone="+919000000000",
            patient_name="Test",
            owner_phone="+919000000001",
            phone_number_id="phone-1",
            service_name="Test",
            resource_name="Dr. Test",
            business_timezone="Asia/Kolkata",
            start_at=dt(2026, 1, 1, 10, 0),  # naive — should fail
        )


# ── Metric emission tests ──────────────────────────────────────────


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_exact_existing_emits_metric(operation: str) -> None:
    metrics.reset()
    service = NotificationService(AsyncMock())
    _allow_nested(service)
    snapshot = _snapshot(operation)
    patient, owner = service._event_values(snapshot)
    service._repo = AsyncMock()
    service._repo.get_event_by_idempotency_key.side_effect = [
        _event(patient, 71),
        _event(owner, 72),
    ]

    await service._verify_or_repair_committed_pair(**_verify_kwargs(snapshot))

    assert (
        metrics.counter_value(
            "notification_reconciliation_total",
            {"operation": operation, "format": "v1", "outcome": "exact_existing"},
        )
        == 1
    )
