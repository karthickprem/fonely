"""Cross-tenant collision, equivalent replay, and owner ambiguity tests."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from fonely.services.notifications import (
    NotificationIdempotencyConflictError,
    NotificationService,
)

NOW = datetime(2026, 8, 12, 10, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import whatsapp_config

    monkeypatch.setattr(
        whatsapp_config.settings,
        "whatsapp_business_mappings",
        '{"phone-1": 1}',
    )


async def test_cross_tenant_collision_on_insert() -> None:
    session = AsyncMock()
    business = MagicMock()
    business.name = "Smile Dental"
    owner = MagicMock()
    owner.phone = "+919000000001"
    session.scalar.return_value = business
    scalars_result = MagicMock()
    scalars_result.all.return_value = [owner]
    session.scalars.return_value = scalars_result
    service = NotificationService(session)
    service._repo = AsyncMock()
    service._repo.insert_event_idempotent.side_effect = [None]
    service._repo.get_event_by_idempotency_key.return_value = None
    other_tenant_event = MagicMock(business_id=999)
    service._repo.get_event_by_global_idempotency_key.return_value = other_tenant_event

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _nested():  # type: ignore[no-untyped-def]
        yield

    session.begin_nested = _nested

    with pytest.raises(NotificationIdempotencyConflictError, match="another business"):
        await service.create_appointment_notifications(
            business_id=1,
            appointment_id=42,
            customer_phone="+919123456789",
            customer_name="Test",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            price=300,
            business_timezone="Asia/Kolkata",
        )


async def test_equivalent_replay_returns_existing_ids() -> None:
    session = AsyncMock()
    business = MagicMock()
    business.name = "Smile Dental"
    owner = MagicMock()
    owner.phone = "+919000000001"
    session.scalar.return_value = business
    scalars_result = MagicMock()
    scalars_result.all.return_value = [owner]
    session.scalars.return_value = scalars_result
    service = NotificationService(session)

    from fonely.services.notifications import NotificationPairSnapshot

    snapshot = NotificationPairSnapshot(
        schema_version=1,
        operation="create",
        business_id=1,
        appointment_id=42,
        clinic_name="Smile Dental",
        patient_phone="+919123456789",
        patient_name="Test",
        owner_phone="+919000000001",
        phone_number_id="phone-1",
        service_name="Consultation",
        resource_name="Dr. Priya",
        business_timezone="Asia/Kolkata",
        start_at=NOW,
        price="300",
    )
    patient_vals, owner_vals = service._event_values(snapshot)
    patient_event = MagicMock(id=10, **patient_vals)
    owner_event = MagicMock(id=11, **owner_vals)

    service._repo = AsyncMock()
    service._repo.insert_event_idempotent.side_effect = [None, None]
    service._repo.get_event_by_idempotency_key.side_effect = [
        patient_event,
        owner_event,
    ]

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _nested():  # type: ignore[no-untyped-def]
        yield

    session.begin_nested = _nested

    ids = await service.create_appointment_notifications(
        business_id=1,
        appointment_id=42,
        customer_phone="+919123456789",
        customer_name="Test",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        price=300,
        business_timezone="Asia/Kolkata",
    )
    assert ids == [10, 11]


async def test_owner_ambiguity_raises() -> None:
    session = AsyncMock()
    business = MagicMock()
    business.name = "Smile Dental"
    session.scalar.return_value = business
    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    session.scalars.return_value = scalars_result
    service = NotificationService(session)

    with pytest.raises(RuntimeError, match="active_owner_not_found"):
        await service.create_appointment_notifications(
            business_id=1,
            appointment_id=42,
            customer_phone="+919123456789",
            customer_name="Test",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            price=300,
            business_timezone="Asia/Kolkata",
        )


async def test_multiple_active_owners_fails_closed() -> None:
    session = AsyncMock()
    business = MagicMock()
    business.name = "Smile Dental"
    owner1 = MagicMock()
    owner1.phone = "+919000000001"
    owner2 = MagicMock()
    owner2.phone = "+919000000002"
    session.scalar.return_value = business
    scalars_result = MagicMock()
    scalars_result.all.return_value = [owner1, owner2]
    session.scalars.return_value = scalars_result
    service = NotificationService(session)

    with pytest.raises(RuntimeError, match="multiple_active_owners"):
        await service.create_appointment_notifications(
            business_id=1,
            appointment_id=42,
            customer_phone="+919123456789",
            customer_name="Test",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            price=300,
            business_timezone="Asia/Kolkata",
        )
