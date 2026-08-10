"""PostgreSQL integration tests for notification outbox transactional guarantees."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.repositories.notifications import NotificationRepository
from fonely.services.notifications import (
    NotificationIdempotencyConflictError,
    NotificationService,
)
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
    assert len(ids2) == 0

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


# ── v1 exact-notification evidence tests ───────────────────────────


async def _assert_pair_has_v1_evidence(events: list[tuple[Any, ...]]) -> None:
    """Assert both events in a pair carry equivalence_snapshot, digest, and schema_version 1."""
    assert len(events) == 2
    for event in events:
        payload = event[0]
        assert isinstance(payload, dict), "payload must be a dict"
        assert payload.get("schema_version") == 1
        assert "equivalence_snapshot" in payload, "missing equivalence_snapshot"
        assert "equivalence_digest" in payload, "missing equivalence_digest"
        snapshot = payload["equivalence_snapshot"]
        assert isinstance(snapshot, dict)
        assert snapshot["schema_version"] == 1


async def test_v1_create_pair_has_equivalence_evidence(
    pg_session: AsyncSession,
) -> None:
    """v1 create pair carries equivalence_snapshot, equivalence_digest, schema_version=1."""
    await _seed_clinic(pg_session)
    service = NotificationService(pg_session)
    event_ids = await service.create_appointment_notifications(
        business_id=1,
        appointment_id=100,
        customer_phone="+919123456789",
        customer_name="EvidenceCreate",
        service_name="Cleaning",
        resource_name="Dr. Asha",
        start_at=NOW,
        price=500,
        business_timezone="Asia/Kolkata",
    )
    assert len(event_ids) == 2

    events = (
        await pg_session.execute(
            text("SELECT payload FROM notification_outbox WHERE entity_id = 100 ORDER BY id")
        )
    ).all()
    await _assert_pair_has_v1_evidence(events)


async def test_v1_cancel_pair_has_equivalence_evidence(
    pg_session: AsyncSession,
) -> None:
    """v1 cancel pair carries equivalence_snapshot, equivalence_digest, schema_version=1."""
    await _seed_clinic(pg_session)
    service = NotificationService(pg_session)
    event_ids = await service.create_cancellation_notifications(
        business_id=1,
        appointment_id=101,
        customer_phone="+919123456789",
        customer_name="EvidenceCancel",
        service_name="Root Canal",
        resource_name="Dr. Asha",
        start_at=NOW,
        business_timezone="Asia/Kolkata",
        reason="Patient requested",
    )
    assert len(event_ids) == 2

    events = (
        await pg_session.execute(
            text("SELECT payload FROM notification_outbox WHERE entity_id = 101 ORDER BY id")
        )
    ).all()
    await _assert_pair_has_v1_evidence(events)


async def test_v1_reschedule_pair_has_equivalence_evidence(
    pg_session: AsyncSession,
) -> None:
    """v1 reschedule pair carries equivalence_snapshot, equivalence_digest, schema_version=1."""
    await _seed_clinic(pg_session)
    service = NotificationService(pg_session)
    old_time = NOW
    new_time = NOW + timedelta(hours=2)
    event_ids = await service.create_reschedule_notifications(
        business_id=1,
        appointment_id=102,
        pending_action_id=7,
        customer_phone="+919123456789",
        customer_name="EvidenceResched",
        service_name="Scaling",
        resource_name="Dr. Asha",
        old_start_at=old_time,
        new_start_at=new_time,
        business_timezone="Asia/Kolkata",
    )
    assert len(event_ids) == 2

    events = (
        await pg_session.execute(
            text("SELECT payload FROM notification_outbox WHERE entity_id = 102 ORDER BY id")
        )
    ).all()
    await _assert_pair_has_v1_evidence(events)
    # Reschedule snapshot must contain pending_action_id
    for event in events:
        snapshot = event[0]["equivalence_snapshot"]
        assert snapshot["pending_action_id"] == 7
        assert snapshot["operation"] == "reschedule"


async def test_v1_replay_returns_original_ids(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """verify_appointment_notifications on committed v1 pair returns same IDs, no new rows."""
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        service = NotificationService(session)
        original_ids = await service.create_appointment_notifications(
            business_id=1,
            appointment_id=103,
            customer_phone="+919123456789",
            customer_name="Replay",
            service_name="Filling",
            resource_name="Dr. Asha",
            start_at=NOW,
            price=200,
            business_timezone="Asia/Kolkata",
        )
        await session.commit()

    assert len(original_ids) == 2

    # Fresh session: verify returns same IDs
    async with pg_session_factory() as verify_session:
        verify_service = NotificationService(verify_session)
        replayed_ids = await verify_service.verify_appointment_notifications(
            business_id=1,
            appointment_id=103,
            customer_phone="+919123456789",
            customer_name="Replay",
            service_name="Filling",
            resource_name="Dr. Asha",
            start_at=NOW,
            price=200,
            business_timezone="Asia/Kolkata",
        )
        assert replayed_ids == original_ids

    # Confirm no new rows were created
    async with pg_session_factory() as count_session:
        total = await count_session.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = 103")
        )
        assert total == 2


async def test_v1_replay_with_mapping_unavailable(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify succeeds even when whatsapp_business_mappings is blank (verify doesn't need it)."""
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        service = NotificationService(session)
        original_ids = await service.create_appointment_notifications(
            business_id=1,
            appointment_id=104,
            customer_phone="+919123456789",
            customer_name="NoMapping",
            service_name="Extraction",
            resource_name="Dr. Asha",
            start_at=NOW,
            price=150,
            business_timezone="Asia/Kolkata",
        )
        await session.commit()

    # Blank mapping
    from fonely.services import whatsapp_config

    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", "")

    async with pg_session_factory() as verify_session:
        verify_service = NotificationService(verify_session)
        replayed_ids = await verify_service.verify_appointment_notifications(
            business_id=1,
            appointment_id=104,
            customer_phone="+919123456789",
            customer_name="NoMapping",
            service_name="Extraction",
            resource_name="Dr. Asha",
            start_at=NOW,
            price=150,
            business_timezone="Asia/Kolkata",
        )
        assert replayed_ids == original_ids


# Legacy payload shape: no equivalence_snapshot, equivalence_digest, schema_version, template_type
def _legacy_patient_payload(operation: str, appointment_id: int) -> dict[str, object]:
    return {
        "clinic_name": "Smile Dental",
        "service": "Cleaning",
        "doctor": "Dr. Asha",
        "date": "Wednesday, Aug 12",
        "time": "7:00 PM",
        "appointment_id": appointment_id,
        "phone_number_id": "phone-1",
        "price": "₹300",
    }


def _legacy_owner_payload(operation: str, appointment_id: int) -> dict[str, object]:
    return {
        "patient_name": "LegacyPatient",
        "patient_phone": "+919123456789",
        "service": "Cleaning",
        "doctor": "Dr. Asha",
        "date": "Wednesday, Aug 12",
        "time": "7:00 PM",
        "appointment_id": appointment_id,
        "phone_number_id": "phone-1",
    }


_LEGACY_KEY_MAP = {
    "create": ("appt-confirm-patient-{aid}", "appt-confirm-owner-{aid}"),
    "cancel": ("appt-cancel-patient-{aid}", "appt-cancel-owner-{aid}"),
    "reschedule": ("appt-resched-patient-{aid}-99", "appt-resched-owner-{aid}-99"),
}

_EVENT_TYPE_MAP = {
    "create": "appointment_confirmed",
    "cancel": "appointment_cancelled",
    "reschedule": "appointment_rescheduled",
}


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_legacy_complete_pair_fails_closed(
    pg_session_factory: async_sessionmaker[AsyncSession],
    operation: str,
) -> None:
    """Legacy rows (no equivalence_snapshot/digest/schema_version) cause legacy_unverifiable."""
    aid = 200 + ["create", "cancel", "reschedule"].index(operation)
    keys = tuple(k.format(aid=aid) for k in _LEGACY_KEY_MAP[operation])
    event_type = _EVENT_TYPE_MAP[operation]

    async with pg_session_factory() as session:
        await _seed_clinic(session)
        repo = NotificationRepository(session)
        await repo.insert_event(
            {
                "business_id": 1,
                "event_type": event_type,
                "entity_type": "appointment",
                "entity_id": aid,
                "recipient_type": "patient",
                "recipient_phone": "+919123456789",
                "channel": "whatsapp",
                "payload": _legacy_patient_payload(operation, aid),
                "status": "pending",
                "idempotency_key": keys[0],
            }
        )
        await repo.insert_event(
            {
                "business_id": 1,
                "event_type": event_type,
                "entity_type": "appointment",
                "entity_id": aid,
                "recipient_type": "owner",
                "recipient_phone": "+914428350001",
                "channel": "whatsapp",
                "payload": _legacy_owner_payload(operation, aid),
                "status": "pending",
                "idempotency_key": keys[1],
            }
        )
        await session.commit()

    async with pg_session_factory() as verify_session:
        verify_service = NotificationService(verify_session)
        with pytest.raises(NotificationIdempotencyConflictError, match="legacy_unverifiable"):
            if operation == "create":
                await verify_service.verify_appointment_notifications(
                    business_id=1,
                    appointment_id=aid,
                    customer_phone="+919123456789",
                    customer_name="LegacyPatient",
                    service_name="Cleaning",
                    resource_name="Dr. Asha",
                    start_at=NOW,
                    price=300,
                    business_timezone="Asia/Kolkata",
                )
            elif operation == "cancel":
                await verify_service.verify_cancellation_notifications(
                    business_id=1,
                    appointment_id=aid,
                    customer_phone="+919123456789",
                    customer_name="LegacyPatient",
                    service_name="Cleaning",
                    resource_name="Dr. Asha",
                    start_at=NOW,
                    business_timezone="Asia/Kolkata",
                )
            else:
                await verify_service.verify_reschedule_notifications(
                    business_id=1,
                    appointment_id=aid,
                    pending_action_id=99,
                    customer_phone="+919123456789",
                    customer_name="LegacyPatient",
                    service_name="Cleaning",
                    resource_name="Dr. Asha",
                    old_start_at=NOW,
                    new_start_at=NOW + timedelta(hours=1),
                    business_timezone="Asia/Kolkata",
                )

        # No new rows written
        count = await verify_session.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = :aid"),
            {"aid": aid},
        )
        assert count == 2


@pytest.mark.parametrize(
    "operation,missing_direction",
    [
        ("create", "owner"),
        ("create", "patient"),
        ("cancel", "owner"),
        ("cancel", "patient"),
        ("reschedule", "owner"),
        ("reschedule", "patient"),
    ],
)
async def test_legacy_one_missing_fails_closed(
    pg_session_factory: async_sessionmaker[AsyncSession],
    operation: str,
    missing_direction: str,
) -> None:
    """Single legacy row (partial pair) causes legacy_unverifiable."""
    aid = (
        210
        + ["create", "cancel", "reschedule"].index(operation) * 2
        + (0 if missing_direction == "owner" else 1)
    )
    keys = tuple(k.format(aid=aid) for k in _LEGACY_KEY_MAP[operation])
    event_type = _EVENT_TYPE_MAP[operation]

    # Insert only the non-missing direction
    if missing_direction == "owner":
        insert_key = keys[0]
        insert_recipient_type = "patient"
        insert_phone = "+919123456789"
        insert_payload = _legacy_patient_payload(operation, aid)
    else:
        insert_key = keys[1]
        insert_recipient_type = "owner"
        insert_phone = "+914428350001"
        insert_payload = _legacy_owner_payload(operation, aid)

    async with pg_session_factory() as session:
        await _seed_clinic(session)
        repo = NotificationRepository(session)
        await repo.insert_event(
            {
                "business_id": 1,
                "event_type": event_type,
                "entity_type": "appointment",
                "entity_id": aid,
                "recipient_type": insert_recipient_type,
                "recipient_phone": insert_phone,
                "channel": "whatsapp",
                "payload": insert_payload,
                "status": "pending",
                "idempotency_key": insert_key,
            }
        )
        await session.commit()

    async with pg_session_factory() as verify_session:
        verify_service = NotificationService(verify_session)
        with pytest.raises(NotificationIdempotencyConflictError, match="legacy_unverifiable"):
            if operation == "create":
                await verify_service.verify_appointment_notifications(
                    business_id=1,
                    appointment_id=aid,
                    customer_phone="+919123456789",
                    customer_name="LegacyPatient",
                    service_name="Cleaning",
                    resource_name="Dr. Asha",
                    start_at=NOW,
                    price=300,
                    business_timezone="Asia/Kolkata",
                )
            elif operation == "cancel":
                await verify_service.verify_cancellation_notifications(
                    business_id=1,
                    appointment_id=aid,
                    customer_phone="+919123456789",
                    customer_name="LegacyPatient",
                    service_name="Cleaning",
                    resource_name="Dr. Asha",
                    start_at=NOW,
                    business_timezone="Asia/Kolkata",
                )
            else:
                await verify_service.verify_reschedule_notifications(
                    business_id=1,
                    appointment_id=aid,
                    pending_action_id=99,
                    customer_phone="+919123456789",
                    customer_name="LegacyPatient",
                    service_name="Cleaning",
                    resource_name="Dr. Asha",
                    old_start_at=NOW,
                    new_start_at=NOW + timedelta(hours=1),
                    business_timezone="Asia/Kolkata",
                )

        # No new rows written
        count = await verify_session.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = :aid"),
            {"aid": aid},
        )
        assert count == 1


async def test_global_idempotency_collision_across_tenants_fails_closed(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Idempotency key from business 1 blocks business 2 from reusing it."""
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        # Create second business
        await session.execute(
            text(
                "INSERT INTO businesses "
                "(id, name, category, primary_contact_phone, timezone, subscription) "
                "VALUES (2, 'Other Dental', 'clinic', '+914428350002', "
                "'Asia/Kolkata', 'trial')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO business_users (business_id, phone, role, is_active) "
                "VALUES (2, '+914428350002', 'owner', true)"
            )
        )

        # Insert a notification for business 1 with a known key
        repo = NotificationRepository(session)
        await repo.insert_event(
            {
                "business_id": 1,
                "event_type": "appointment_confirmed",
                "entity_type": "appointment",
                "entity_id": 300,
                "recipient_type": "patient",
                "recipient_phone": "+919123456789",
                "channel": "whatsapp",
                "payload": {"schema_version": 1, "template_type": "appointment_confirmed"},
                "status": "pending",
                "idempotency_key": "appt-confirm-patient-300",
            }
        )
        await session.commit()

    # Business 2 tries to create appointment notifications with same appointment_id
    # which produces the same idempotency key
    async with pg_session_factory() as session2:
        service = NotificationService(session2)
        with pytest.raises(NotificationIdempotencyConflictError, match="another business"):
            await service.create_appointment_notifications(
                business_id=2,
                appointment_id=300,
                customer_phone="+919000000000",
                customer_name="CrossTenant",
                service_name="Checkup",
                resource_name="Dr. Other",
                start_at=NOW,
                price=100,
                business_timezone="Asia/Kolkata",
            )
