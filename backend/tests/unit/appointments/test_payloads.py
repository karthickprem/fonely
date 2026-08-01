"""Appointment PendingAction payload and confirmation tests."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from fonely.domain.pending_actions.payloads import PendingAppointmentEnvelope, validate_payload
from fonely.domain.pending_actions.snapshots import confirmation_snapshot, payload_digest
from fonely.models.enums import PendingActionType


def facts() -> dict[str, object]:
    return {
        "service_id": 10,
        "service_name": "Consultation",
        "resource_id": 20,
        "resource_name": "Resource One",
        "start_at": "2026-08-03T10:00:00+05:30",
        "end_at": "2026-08-03T10:30:00+05:30",
        "effective_start_at": "2026-08-03T09:55:00+05:30",
        "effective_end_at": "2026-08-03T10:40:00+05:30",
        "duration_minutes": 30,
        "buffer_before_minutes": 5,
        "buffer_after_minutes": 10,
        "price": "500.00",
        "business_timezone": "Asia/Kolkata",
    }


def create_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_type": "appointment",
        "data": {
            "operation": "create",
            "facts": facts(),
            "customer_name": "Example",
            "customer_phone": "+919123456789",
            "reason": None,
            "call_id": 9,
        },
    }


def test_valid_create_cancel_and_reschedule() -> None:
    create = validate_payload(PendingActionType.APPOINTMENT, 1, create_payload())
    assert isinstance(create, PendingAppointmentEnvelope)

    cancel = create_payload()
    cancel["data"] = {
        "operation": "cancel",
        "target_appointment_id": 1,
        "target_expected_version": 2,
        "current_facts": facts(),
        "reason_code": "customer_request",
    }
    validate_payload(PendingActionType.APPOINTMENT, 1, cancel)

    reschedule = create_payload()
    new_facts = facts()
    new_facts.update(
        {
            "start_at": "2026-08-03T11:00:00+05:30",
            "end_at": "2026-08-03T11:30:00+05:30",
            "effective_start_at": "2026-08-03T10:55:00+05:30",
            "effective_end_at": "2026-08-03T11:40:00+05:30",
        }
    )
    reschedule["data"] = {
        "operation": "reschedule",
        "target_appointment_id": 1,
        "target_expected_version": 2,
        "old_facts": facts(),
        "new_facts": new_facts,
    }
    validate_payload(PendingActionType.APPOINTMENT, 1, reschedule)


@pytest.mark.parametrize("equivalent_offset", [False, True])
def test_reschedule_rejects_canonical_no_op(equivalent_offset: bool) -> None:
    old_facts = facts()
    new_facts = deepcopy(old_facts)
    if equivalent_offset:
        new_facts.update(
            {
                "start_at": "2026-08-03T04:30:00Z",
                "end_at": "2026-08-03T05:00:00Z",
                "effective_start_at": "2026-08-03T04:25:00Z",
                "effective_end_at": "2026-08-03T05:10:00Z",
            }
        )
    payload = create_payload()
    payload["data"] = {
        "operation": "reschedule",
        "target_appointment_id": 1,
        "target_expected_version": 2,
        "old_facts": old_facts,
        "new_facts": new_facts,
    }

    with pytest.raises(ValidationError, match="must change"):
        validate_payload(PendingActionType.APPOINTMENT, 1, payload)


@pytest.mark.parametrize("field", ["target_appointment_id", "target_expected_version"])
def test_mutation_payload_integer_fields_use_postgresql_bounds(field: str) -> None:
    payload = cancellation_payload("customer_request")
    data = payload["data"]
    assert isinstance(data, dict)
    data[field] = 2_147_483_647
    validate_payload(PendingActionType.APPOINTMENT, 1, payload)

    data[field] = 2_147_483_648
    with pytest.raises(ValidationError):
        validate_payload(PendingActionType.APPOINTMENT, 1, payload)


def test_unknown_operation_field_rejected() -> None:
    payload = create_payload()
    data = payload["data"]
    assert isinstance(data, dict)
    data["target_appointment_id"] = 1
    with pytest.raises(ValidationError):
        validate_payload(PendingActionType.APPOINTMENT, 1, payload)


def test_naive_and_inconsistent_derived_times_rejected() -> None:
    payload = create_payload()
    data = payload["data"]
    assert isinstance(data, dict)
    appointment_facts = data["facts"]
    assert isinstance(appointment_facts, dict)
    appointment_facts["start_at"] = "2026-08-03T10:00:00"
    with pytest.raises(ValidationError, match="timezone-aware"):
        validate_payload(PendingActionType.APPOINTMENT, 1, payload)

    payload = create_payload()
    data = payload["data"]
    assert isinstance(data, dict)
    appointment_facts = data["facts"]
    assert isinstance(appointment_facts, dict)
    appointment_facts["end_at"] = "2026-08-03T11:00:00+05:30"
    with pytest.raises(ValidationError, match="does not match duration"):
        validate_payload(PendingActionType.APPOINTMENT, 1, payload)


def test_dst_gap_and_business_offset_mismatch_rejected() -> None:
    payload = create_payload()
    data = payload["data"]
    assert isinstance(data, dict)
    appointment_facts = data["facts"]
    assert isinstance(appointment_facts, dict)
    appointment_facts.update(
        {
            "start_at": "2026-03-08T02:30:00-05:00",
            "end_at": "2026-03-08T03:00:00-05:00",
            "effective_start_at": "2026-03-08T02:25:00-05:00",
            "effective_end_at": "2026-03-08T03:10:00-05:00",
            "business_timezone": "America/New_York",
        }
    )
    with pytest.raises(ValidationError, match="does not match business timezone"):
        validate_payload(PendingActionType.APPOINTMENT, 1, payload)

    payload = create_payload()
    data = payload["data"]
    assert isinstance(data, dict)
    appointment_facts = data["facts"]
    assert isinstance(appointment_facts, dict)
    appointment_facts["start_at"] = "2026-08-03T10:00:00+04:30"
    with pytest.raises(ValidationError, match="does not match business timezone"):
        validate_payload(PendingActionType.APPOINTMENT, 1, payload)


def test_equivalent_utc_instants_have_same_digest() -> None:
    first = validate_payload(PendingActionType.APPOINTMENT, 1, create_payload())
    second_raw = deepcopy(create_payload())
    data = second_raw["data"]
    assert isinstance(data, dict)
    appointment_facts = data["facts"]
    assert isinstance(appointment_facts, dict)
    appointment_facts.update(
        {
            "start_at": "2026-08-03T04:30:00Z",
            "end_at": "2026-08-03T05:00:00Z",
            "effective_start_at": "2026-08-03T04:25:00Z",
            "effective_end_at": "2026-08-03T05:10:00Z",
        }
    )
    second = validate_payload(PendingActionType.APPOINTMENT, 1, second_raw)
    assert payload_digest(first) == payload_digest(second)


def test_resource_change_changes_digest() -> None:
    first = validate_payload(PendingActionType.APPOINTMENT, 1, create_payload())
    changed_raw = deepcopy(create_payload())
    data = changed_raw["data"]
    assert isinstance(data, dict)
    appointment_facts = data["facts"]
    assert isinstance(appointment_facts, dict)
    appointment_facts["resource_id"] = 21
    changed = validate_payload(PendingActionType.APPOINTMENT, 1, changed_raw)
    assert payload_digest(first) != payload_digest(changed)


@pytest.mark.parametrize("timezone", ["Asia/Kolkata"])
def test_valid_iana_timezone_accepted(timezone: str) -> None:
    payload = create_payload()
    data = payload["data"]
    assert isinstance(data, dict)
    appointment_facts = data["facts"]
    assert isinstance(appointment_facts, dict)
    appointment_facts["business_timezone"] = timezone
    validated = validate_payload(PendingActionType.APPOINTMENT, 1, payload)
    assert validated.data.facts.business_timezone == timezone


@pytest.mark.parametrize(
    "timezone", ["Mars/Salon", "localtime", "Factory", "posixrules", "posix/UTC", "right/UTC"]
)
def test_invalid_timezone_rejected(timezone: str) -> None:
    payload = create_payload()
    data = payload["data"]
    assert isinstance(data, dict)
    appointment_facts = data["facts"]
    assert isinstance(appointment_facts, dict)
    appointment_facts["business_timezone"] = timezone
    with pytest.raises(ValidationError, match="Invalid timezone"):
        validate_payload(PendingActionType.APPOINTMENT, 1, payload)


def cancellation_payload(reason_code: str | None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_type": "appointment",
        "data": {
            "operation": "cancel",
            "target_appointment_id": 1,
            "target_expected_version": 2,
            "current_facts": facts(),
            "reason_code": reason_code,
        },
    }


def test_cancellation_confirmation_includes_safe_reason() -> None:
    first = validate_payload(
        PendingActionType.APPOINTMENT, 1, cancellation_payload("customer_request")
    )
    second = validate_payload(
        PendingActionType.APPOINTMENT, 1, cancellation_payload("duplicate_booking")
    )
    first_snapshot = json.loads(confirmation_snapshot(first))
    second_snapshot = json.loads(confirmation_snapshot(second))
    assert first_snapshot["facts"]["reason_code"] == "customer_request"
    assert first_snapshot != second_snapshot
    assert "target_expected_version" not in first_snapshot["facts"]
    assert "effective_start_at" not in first_snapshot["facts"]


def test_null_cancellation_reason_is_explicit() -> None:
    payload = validate_payload(PendingActionType.APPOINTMENT, 1, cancellation_payload(None))
    snapshot = json.loads(confirmation_snapshot(payload))
    assert snapshot["facts"]["reason_code"] is None


def test_confirmation_snapshot_excludes_private_and_internal_facts() -> None:
    payload = validate_payload(PendingActionType.APPOINTMENT, 1, create_payload())
    snapshot = json.loads(confirmation_snapshot(payload))
    facts_value = snapshot["facts"]
    assert facts_value["operation"] == "create"
    assert facts_value["resource_name"] == "Resource One"
    assert "customer_phone" not in facts_value
    assert "call_id" not in facts_value
    assert "effective_start_at" not in facts_value
