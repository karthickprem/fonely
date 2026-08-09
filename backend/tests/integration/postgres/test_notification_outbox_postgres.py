"""PostgreSQL integration tests for notification outbox transactional guarantees."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.repositories.notifications import NotificationRepository
from fonely.services.notifications import NotificationService
from fonely.workers.notification_worker import (
    LoggingNotificationSender,
    run_notification_worker,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 12, 13, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import whatsapp_config

    monkeypatch.setattr(
        whatsapp_config.settings,
        "whatsapp_business_mappings",
        '{"phone-1": 1}',
    )


async def _seed_clinic(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental', 'clinic', '+914428350001', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (1, '+914428350001', 'owner', true)"
        )
    )


async def test_appointment_notifications_created_in_same_transaction(
    pg_session: AsyncSession,
) -> None:
    """Functional proof A+B: outbox events exist in same transaction as appointment data."""
    await _seed_clinic(pg_session)

    service = NotificationService(pg_session)
    event_ids = await service.create_appointment_notifications(
        business_id=1,
        appointment_id=42,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="General Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        price=300,
        business_timezone="Asia/Kolkata",
    )
    assert len(event_ids) == 2

    events = (
        await pg_session.execute(
            text(
                "SELECT id, event_type, recipient_type, recipient_phone, status, "
                "idempotency_key, payload "
                "FROM notification_outbox WHERE entity_id = 42 ORDER BY id"
            )
        )
    ).all()
    assert len(events) == 2

    patient = events[0]
    assert patient[1] == "appointment_confirmed"
    assert patient[2] == "patient"
    assert patient[3] == "+919123456789"
    assert patient[4] == "pending"
    assert patient[5] == "appt-confirm-patient-42"
    assert patient[6]["clinic_name"] == "Smile Dental"
    assert patient[6]["service"] == "General Consultation"
    assert patient[6]["doctor"] == "Dr. Priya"
    assert patient[6]["appointment_id"] == 42

    owner = events[1]
    assert owner[2] == "owner"
    assert owner[3] == "+914428350001"
    assert owner[5] == "appt-confirm-owner-42"
    assert owner[6]["patient_name"] == "Karthick"


async def test_outbox_rollback_with_transaction(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Functional proof E: rollback removes outbox events."""
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        await session.commit()

    async with pg_session_factory() as session:
        service = NotificationService(session)
        await service.create_appointment_notifications(
            business_id=1,
            appointment_id=99,
            customer_phone="+919000000000",
            customer_name="Test",
            service_name="Test Service",
            resource_name="Dr. Test",
            start_at=NOW,
            price=100,
            business_timezone="Asia/Kolkata",
        )
        count_before_rollback = await session.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = 99")
        )
        assert count_before_rollback == 2
        await session.rollback()

    async with pg_session_factory() as verify:
        count_after_rollback = await verify.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = 99")
        )
        assert count_after_rollback == 0


async def test_worker_delivers_and_marks_events(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Functional proof C+D: worker claims, delivers, marks delivered."""
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        service = NotificationService(session)
        await service.create_appointment_notifications(
            business_id=1,
            appointment_id=50,
            customer_phone="+919123456789",
            customer_name="Patient",
            service_name="Scaling",
            resource_name="Dr. Priya",
            start_at=NOW,
            price=800,
            business_timezone="Asia/Kolkata",
        )
        await session.commit()

    sender = LoggingNotificationSender()
    await run_notification_worker(pg_session_factory, sender, max_iterations=1, batch_size=10)

    async with pg_session_factory() as verify:
        events = (
            await verify.execute(
                text(
                    "SELECT status, delivered_at FROM notification_outbox "
                    "WHERE entity_id = 50 ORDER BY id"
                )
            )
        ).all()
        assert len(events) == 2
        for status, delivered_at in events:
            assert status == "delivered"
            assert delivered_at is not None


async def test_worker_retries_with_backoff(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        repo = NotificationRepository(session)
        await repo.insert_event(
            {
                "business_id": 1,
                "event_type": "appointment_confirmed",
                "entity_type": "appointment",
                "entity_id": 60,
                "recipient_type": "patient",
                "recipient_phone": "+919000000001",
                "channel": "whatsapp",
                "payload": {"test": True},
                "status": "pending",
                "idempotency_key": "retry-test-60",
            }
        )
        await session.commit()

    class FailingSender:
        async def send(self, event: object) -> None:
            raise ConnectionError("network down")

    await run_notification_worker(
        pg_session_factory,
        FailingSender(),
        max_iterations=1,
        batch_size=10,  # type: ignore[arg-type]
    )

    async with pg_session_factory() as verify:
        event = (
            await verify.execute(
                text(
                    "SELECT status, attempts, last_error, next_attempt_at "
                    "FROM notification_outbox WHERE idempotency_key = 'retry-test-60'"
                )
            )
        ).one()
        assert event[0] == "failed"
        assert event[1] == 1
        assert event[2] == "ConnectionError"
        assert event[3] is not None


async def test_dead_letter_after_max_attempts(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        repo = NotificationRepository(session)
        await repo.insert_event(
            {
                "business_id": 1,
                "event_type": "appointment_confirmed",
                "entity_type": "appointment",
                "entity_id": 70,
                "recipient_type": "patient",
                "recipient_phone": "+919000000002",
                "channel": "whatsapp",
                "payload": {"test": True},
                "status": "failed",
                "attempts": 4,
                "max_attempts": 5,
                "idempotency_key": "dead-letter-70",
                "next_attempt_at": datetime.now(UTC) - timedelta(minutes=1),
            }
        )
        await session.commit()

    class FailingSender:
        async def send(self, event: object) -> None:
            raise ConnectionError("still down")

    await run_notification_worker(
        pg_session_factory,
        FailingSender(),
        max_iterations=1,
        batch_size=10,  # type: ignore[arg-type]
    )

    async with pg_session_factory() as verify:
        event = (
            await verify.execute(
                text(
                    "SELECT status, attempts FROM notification_outbox "
                    "WHERE idempotency_key = 'dead-letter-70'"
                )
            )
        ).one()
        assert event[0] == "dead_letter"
        assert event[1] == 5


async def test_idempotent_notification_creation(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session)
    service = NotificationService(pg_session)

    ids1 = await service.create_appointment_notifications(
        business_id=1,
        appointment_id=80,
        customer_phone="+919123456789",
        customer_name="Dup",
        service_name="Test",
        resource_name="Dr. Test",
        start_at=NOW,
        price=100,
        business_timezone="Asia/Kolkata",
    )
    ids2 = await service.create_appointment_notifications(
        business_id=1,
        appointment_id=80,
        customer_phone="+919123456789",
        customer_name="Dup",
        service_name="Test",
        resource_name="Dr. Test",
        start_at=NOW,
        price=100,
        business_timezone="Asia/Kolkata",
    )
    assert len(ids1) == 2
    assert ids2 == ids1

    total = await pg_session.scalar(
        text("SELECT count(*) FROM notification_outbox WHERE entity_id = 80")
    )
    assert total == 2


async def test_tenant_isolation(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session)
    await pg_session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (2, 'Other Clinic', 'clinic', '+919999999999', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    repo = NotificationRepository(pg_session)
    await repo.insert_event(
        {
            "business_id": 1,
            "event_type": "appointment_confirmed",
            "entity_type": "appointment",
            "entity_id": 90,
            "recipient_type": "patient",
            "recipient_phone": "+919000000003",
            "channel": "whatsapp",
            "payload": {},
            "status": "pending",
            "idempotency_key": "tenant-iso-90",
        }
    )

    events_b1 = await repo.get_events_for_entity(1, "appointment", 90)
    events_b2 = await repo.get_events_for_entity(2, "appointment", 90)
    assert len(events_b1) == 1
    assert len(events_b2) == 0


async def test_global_idempotency_collision_across_tenants_fails_closed(
    pg_session: AsyncSession,
) -> None:
    await _seed_clinic(pg_session)
    await pg_session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (2, 'Other Clinic', 'clinic', '+919999999999', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await pg_session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (2, '+919999999998', 'owner', true)"
        )
    )
    repo = NotificationRepository(pg_session)
    await repo.insert_event(
        {
            "business_id": 1,
            "event_type": "appointment_confirmed",
            "entity_type": "appointment",
            "entity_id": 99,
            "recipient_type": "patient",
            "recipient_phone": "+919000000003",
            "channel": "whatsapp",
            "payload": {"appointment_id": 99, "phone_number_id": "phone-1"},
            "status": "pending",
            "idempotency_key": "shared-semantic-key",
        }
    )
    service = NotificationService(pg_session)
    values = {
        "business_id": 2,
        "event_type": "appointment_confirmed",
        "entity_type": "appointment",
        "entity_id": 99,
        "recipient_type": "patient",
        "recipient_phone": "+919999999997",
        "recipient_name": None,
        "channel": "whatsapp",
        "payload": {"appointment_id": 99, "phone_number_id": "phone-2"},
        "status": "pending",
        "idempotency_key": "shared-semantic-key",
    }

    from fonely.services.notifications import NotificationIdempotencyConflictError

    with pytest.raises(NotificationIdempotencyConflictError, match="another business"):
        await service._insert_or_verify(values)

    assert await repo.get_event_by_idempotency_key(2, "shared-semantic-key") is None
    assert (await repo.get_event_by_global_idempotency_key("shared-semantic-key")).business_id == 1


def _legacy_pair_values(operation: str) -> tuple[dict[str, object], dict[str, object]]:
    local = NOW.astimezone(ZoneInfo("Asia/Kolkata"))
    common = {
        "service": "Consultation",
        "doctor": "Dr. Priya",
        "appointment_id": 777,
        "phone_number_id": "legacy-phone",
    }
    patient_payload = {"clinic_name": "Smile Dental", **common}
    owner_payload = {
        "patient_name": "Legacy Patient",
        "patient_phone": "+919123456789",
        **common,
    }
    if operation in {"create", "cancel"}:
        for payload in (patient_payload, owner_payload):
            payload.update(
                date=local.strftime("%A, %b %d"),
                time=local.strftime("%-I:%M %p"),
            )
    if operation == "create":
        event_type = "appointment_confirmed"
        patient_payload["price"] = "₹300"
        key = "appt-confirm-{recipient}-777"
    elif operation == "cancel":
        event_type = "appointment_cancelled"
        patient_payload["reason"] = "Requested"
        owner_payload["reason"] = "Requested"
        key = "appt-cancel-{recipient}-777"
    else:
        event_type = "appointment_rescheduled"
        new_local = (NOW + timedelta(hours=2)).astimezone(ZoneInfo("Asia/Kolkata"))
        for payload in (patient_payload, owner_payload):
            payload.update(
                old_date=local.strftime("%A, %b %d"),
                old_time=local.strftime("%-I:%M %p"),
                new_date=new_local.strftime("%A, %b %d"),
                new_time=new_local.strftime("%-I:%M %p"),
            )
        key = "appt-resched-{recipient}-777-44"
    base = {
        "business_id": 1,
        "event_type": event_type,
        "entity_type": "appointment",
        "entity_id": 777,
        "channel": "whatsapp",
        "status": "pending",
    }
    return (
        {
            **base,
            "recipient_type": "patient",
            "recipient_phone": "+919123456789",
            "recipient_name": "Legacy Patient",
            "payload": patient_payload,
            "idempotency_key": key.format(recipient="patient"),
        },
        {
            **base,
            "recipient_type": "owner",
            "recipient_phone": "+914428350001",
            "recipient_name": None,
            "payload": owner_payload,
            "idempotency_key": key.format(recipient="owner"),
        },
    )


async def _verify_legacy(service: NotificationService, operation: str) -> list[int]:
    common = {
        "business_id": 1,
        "appointment_id": 777,
        "customer_phone": "+919123456789",
        "customer_name": "Legacy Patient",
        "service_name": "Consultation",
        "resource_name": "Dr. Priya",
        "business_timezone": "Asia/Kolkata",
    }
    if operation == "create":
        return await service.verify_appointment_notifications(**common, start_at=NOW, price=300)
    if operation == "cancel":
        return await service.verify_cancellation_notifications(
            **common, start_at=NOW, reason="Requested"
        )
    return await service.verify_reschedule_notifications(
        **common,
        pending_action_id=44,
        old_start_at=NOW,
        new_start_at=NOW + timedelta(hours=2),
    )


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_complete_populated_legacy_pair_replays_exactly(
    pg_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    await _seed_clinic(pg_session)
    repo = NotificationRepository(pg_session)
    patient, owner = _legacy_pair_values(operation)
    patient_row = await repo.insert_event(patient)
    owner_row = await repo.insert_event(owner)
    await pg_session.flush()

    from fonely.services import notifications, whatsapp_config

    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", "")
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", "")
    assert await _verify_legacy(NotificationService(pg_session), operation) == [
        patient_row.id,
        owner_row.id,
    ]


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
@pytest.mark.parametrize("missing", ["patient", "owner"])
async def test_incomplete_populated_legacy_pair_fails_without_repair(
    pg_session: AsyncSession,
    operation: str,
    missing: str,
) -> None:
    await _seed_clinic(pg_session)
    repo = NotificationRepository(pg_session)
    patient, owner = _legacy_pair_values(operation)
    await repo.insert_event(owner if missing == "patient" else patient)
    await pg_session.flush()
    before = await pg_session.scalar(text("SELECT count(*) FROM notification_outbox"))

    from fonely.services.notifications import NotificationIdempotencyConflictError

    with pytest.raises(NotificationIdempotencyConflictError, match="cannot be reconstructed"):
        await _verify_legacy(NotificationService(pg_session), operation)
    after = await pg_session.scalar(text("SELECT count(*) FROM notification_outbox"))
    assert after == before == 1
