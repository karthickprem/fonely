"""Unit tests for notification recipient resolution and snapshot equivalence."""

import pytest
from pydantic import ValidationError

from fonely.services.notifications import (
    NotificationSnapshot,
    ResolvedRecipient,
    _canonical_digest,
)


def _snapshot(**overrides) -> NotificationSnapshot:
    defaults = {
        "operation": "create",
        "business_id": 1,
        "appointment_id": 100,
        "recipient_type": "patient",
        "recipient_phone": "+919123456789",
        "clinic_name": "Smile Dental",
        "patient_phone": "+919123456789",
        "patient_name": "Karthick",
        "service_name": "Consultation",
        "resource_name": "Dr. Priya",
        "business_timezone": "Asia/Kolkata",
        "start_at": "2026-08-15T04:30:00+00:00",
        "phone_number_id": "phone-1",
    }
    defaults.update(overrides)
    return NotificationSnapshot(**defaults)


class TestCanonicalDigest:
    def test_deterministic(self) -> None:
        s = _snapshot()
        assert _canonical_digest(s) == _canonical_digest(s)

    def test_different_field_produces_different_digest(self) -> None:
        a = _snapshot(patient_name="Karthick")
        b = _snapshot(patient_name="Priya")
        assert _canonical_digest(a) != _canonical_digest(b)

    def test_digest_is_sha256_hex(self) -> None:
        s = _snapshot()
        d = _canonical_digest(s)
        assert len(d) == 64
        int(d, 16)


class TestNotificationSnapshot:
    def test_schema_version_default(self) -> None:
        s = _snapshot()
        assert s.schema_version == 1

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _snapshot(unexpected_field="value")

    def test_roundtrip_json(self) -> None:
        s = _snapshot()
        dumped = s.model_dump(mode="json")
        restored = NotificationSnapshot(**dumped)
        assert restored == s
        assert _canonical_digest(restored) == _canonical_digest(s)


class TestResolvedRecipient:
    def test_patient_has_no_bu_id(self) -> None:
        r = ResolvedRecipient(
            recipient_type="patient",
            phone="+919123456789",
            name="Karthick",
            bu_id=None,
        )
        assert r.bu_id is None

    def test_owner_has_bu_id(self) -> None:
        r = ResolvedRecipient(
            recipient_type="owner",
            phone="+919000000001",
            name=None,
            bu_id=5,
        )
        assert r.bu_id == 5


class TestIdempotencyKey:
    def test_patient_key_format(self) -> None:
        from fonely.services.notifications import NotificationService

        svc = NotificationService.__new__(NotificationService)
        r = ResolvedRecipient("patient", "+919123456789", "K", None)
        key = svc._idempotency_key("create", 100, r)
        assert key == "notif-create-patient-100"

    def test_owner_key_includes_bu_id(self) -> None:
        from fonely.services.notifications import NotificationService

        svc = NotificationService.__new__(NotificationService)
        r = ResolvedRecipient("owner", "+919000000001", None, 5)
        key = svc._idempotency_key("cancel", 200, r)
        assert key == "notif-cancel-owner-200-bu5"

    def test_different_owners_get_different_keys(self) -> None:
        from fonely.services.notifications import NotificationService

        svc = NotificationService.__new__(NotificationService)
        r1 = ResolvedRecipient("owner", "+919000000001", None, 1)
        r2 = ResolvedRecipient("owner", "+919000000002", None, 2)
        k1 = svc._idempotency_key("create", 100, r1)
        k2 = svc._idempotency_key("create", 100, r2)
        assert k1 != k2


class TestSnapshotPayload:
    def test_payload_contains_equivalence(self) -> None:
        from fonely.services.notifications import NotificationService

        svc = NotificationService.__new__(NotificationService)
        s = _snapshot()
        payload = svc._snapshot_to_payload(s)
        assert "equivalence_snapshot" in payload
        assert "equivalence_digest" in payload
        assert payload["schema_version"] == 1
        assert payload["equivalence_digest"] == _canonical_digest(s)

    def test_patient_payload_has_clinic_name(self) -> None:
        from fonely.services.notifications import NotificationService

        svc = NotificationService.__new__(NotificationService)
        s = _snapshot(recipient_type="patient")
        payload = svc._snapshot_to_payload(s)
        assert payload["clinic_name"] == "Smile Dental"
        assert "patient_name" not in payload

    def test_owner_payload_has_patient_info(self) -> None:
        from fonely.services.notifications import NotificationService

        svc = NotificationService.__new__(NotificationService)
        s = _snapshot(recipient_type="owner")
        payload = svc._snapshot_to_payload(s)
        assert payload["patient_name"] == "Karthick"
        assert payload["patient_phone"] == "+919123456789"
        assert "clinic_name" not in payload

    def test_reschedule_payload_has_old_and_new_times(self) -> None:
        from fonely.services.notifications import NotificationService

        svc = NotificationService.__new__(NotificationService)
        s = _snapshot(
            operation="reschedule",
            recipient_type="patient",
            old_start_at="2026-08-15T04:30:00+00:00",
            new_start_at="2026-08-15T06:30:00+00:00",
        )
        payload = svc._snapshot_to_payload(s)
        assert "old_time" in payload
        assert "new_time" in payload
        assert "old_date" in payload
        assert "new_date" in payload
