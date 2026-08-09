"""Notification tenant scope, recipient authority, and replay equivalence tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from fonely.services.notifications import (
    NotificationIdempotencyConflictError,
    NotificationService,
)


def _values() -> dict[str, object]:
    return {
        "business_id": 1,
        "event_type": "appointment_cancelled",
        "entity_type": "appointment",
        "entity_id": 9,
        "recipient_type": "patient",
        "recipient_phone": "+919123456789",
        "recipient_name": "Patient",
        "channel": "whatsapp",
        "payload": {"appointment_id": 9, "phone_number_id": "phone-1"},
        "status": "pending",
        "idempotency_key": "same-global-key",
    }


async def test_cross_tenant_global_key_collision_fails_closed() -> None:
    service = NotificationService(AsyncMock())
    service._repo = AsyncMock()
    service._repo.insert_event_idempotent.return_value = None
    service._repo.get_event_by_idempotency_key.return_value = None
    service._repo.get_event_by_global_idempotency_key.return_value = MagicMock(business_id=2)

    with pytest.raises(NotificationIdempotencyConflictError, match="another business"):
        await service._insert_or_verify(_values())

    service._repo.get_event_by_idempotency_key.assert_awaited_once_with(1, "same-global-key")


async def test_equivalent_notification_replay_returns_existing_id() -> None:
    service = NotificationService(AsyncMock())
    service._repo = AsyncMock()
    service._repo.insert_event_idempotent.return_value = None
    service._repo.get_event_by_idempotency_key.return_value = MagicMock(id=77, **_values())

    assert await service._insert_or_verify(_values()) == 77


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recipient_phone", "+919000000000"),
        ("recipient_name", "Other"),
        ("channel", "internal"),
        ("event_type", "appointment_confirmed"),
        ("status", "invalid"),
        ("payload", {"appointment_id": 9, "phone_number_id": "stale"}),
    ],
)
async def test_non_equivalent_notification_replay_is_conflict(field: str, value: object) -> None:
    service = NotificationService(AsyncMock())
    service._repo = AsyncMock()
    service._repo.insert_event_idempotent.return_value = None
    existing = MagicMock(id=77, **_values())
    setattr(existing, field, value)
    service._repo.get_event_by_idempotency_key.return_value = existing

    with pytest.raises(NotificationIdempotencyConflictError, match="non-equivalent"):
        await service._insert_or_verify(_values())


async def test_owner_recipient_requires_one_active_owner() -> None:
    session = AsyncMock()
    business = MagicMock(name="Clinic")
    business.name = "Clinic"
    session.scalar.return_value = business
    scalars = MagicMock()
    scalars.all.return_value = []
    session.scalars.return_value = scalars
    service = NotificationService(session)

    with pytest.raises(RuntimeError, match="active_owner_recipient_ambiguous"):
        await service._business_and_owner_phone(1)


async def test_owner_recipient_rejects_multiple_active_owners() -> None:
    session = AsyncMock()
    business = MagicMock(name="Clinic")
    business.name = "Clinic"
    session.scalar.return_value = business
    scalars = MagicMock()
    scalars.all.return_value = [
        MagicMock(phone="+919111111111"),
        MagicMock(phone="+919222222222"),
    ]
    session.scalars.return_value = scalars
    service = NotificationService(session)

    with pytest.raises(RuntimeError, match="active_owner_recipient_ambiguous"):
        await service._business_and_owner_phone(1)


async def test_owner_recipient_uses_active_membership_not_business_metadata() -> None:
    session = AsyncMock()
    business = MagicMock(name="Clinic", primary_contact_phone="+910000000000")
    business.name = "Clinic"
    owner = MagicMock(phone="+919999999999")
    session.scalar.return_value = business
    scalars = MagicMock()
    scalars.all.return_value = [owner]
    session.scalars.return_value = scalars
    service = NotificationService(session)

    resolved_business, phone = await service._business_and_owner_phone(1)

    assert resolved_business is business
    assert phone == "+919999999999"
