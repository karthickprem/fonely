"""Tests for exact v1 payload equality — extra keys must be rejected."""

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fonely.services.notifications import NotificationPairSnapshot, NotificationService

NOW = datetime(2026, 8, 12, 10, tzinfo=UTC)


def _snapshot() -> NotificationPairSnapshot:
    return NotificationPairSnapshot(
        schema_version=1,
        operation="create",
        business_id=1,
        appointment_id=9,
        clinic_name="Smile Dental",
        patient_phone="+919123456789",
        patient_name="Patient",
        owner_phone="+919000000001",
        phone_number_id="phone-1",
        service_name="Consultation",
        resource_name="Dr. Priya",
        business_timezone="Asia/Kolkata",
        start_at=NOW,
        price="300",
    )


def test_extra_payload_key_rejected() -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot()
    expected, _ = service._event_values(snapshot)
    persisted = deepcopy(expected)
    payload = persisted["payload"]
    assert isinstance(payload, dict)
    payload["unexpected_extra_key"] = "injected"
    assert service._event_equivalent(MagicMock(**persisted, id=1), expected) is False


def test_exact_payload_accepted() -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot()
    expected, _ = service._event_values(snapshot)
    persisted = deepcopy(expected)
    assert service._event_equivalent(MagicMock(**persisted, id=1), expected) is True


def test_missing_payload_key_rejected() -> None:
    service = NotificationService(AsyncMock())
    snapshot = _snapshot()
    expected, _ = service._event_values(snapshot)
    persisted = deepcopy(expected)
    payload = persisted["payload"]
    assert isinstance(payload, dict)
    del payload["clinic_name"]
    assert service._event_equivalent(MagicMock(**persisted, id=1), expected) is False
