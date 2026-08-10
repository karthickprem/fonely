"""PostgreSQL evidence for transactional appointment notifications.

Covers: multi-owner recipients, atomicity, concurrency, replay with
config mutation, corrupted evidence fail-closed, legacy compatibility,
and zero-owner fail-closed.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.services.notifications import (
    NotificationConfigurationError,
    NotificationEvidenceConflictError,
    NotificationService,
)

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import notifications, whatsapp_config

    mappings = '{"phone-1": 1}'
    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_phone_number_id", "phone-1")


NOW = datetime(2026, 8, 15, 4, 30, tzinfo=UTC)


async def _seed_clinic(
    session: AsyncSession,
    *,
    owner_phones: list[str] | None = None,
    business_id: int = 1,
) -> None:
    if owner_phones is None:
        owner_phones = ["+919000000001"]
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:id, 'Smile Dental', 'clinic', :phone, 'Asia/Kolkata', 'trial')"
        ),
        {"id": business_id, "phone": owner_phones[0]},
    )
    for i, phone in enumerate(owner_phones, start=1):
        await session.execute(
            text(
                "INSERT INTO business_users (id, business_id, phone, role, is_active) "
                "VALUES (:uid, :bid, :phone, 'owner', true)"
            ),
            {"uid": business_id * 100 + i, "bid": business_id, "phone": phone},
        )
    await session.flush()


# =========================================================================
# 1. MULTI-OWNER EVENT SET
# =========================================================================


async def test_single_owner_produces_two_events(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session)
    svc = NotificationService(pg_session)
    ids = await svc.create_appointment_notifications(
        business_id=1,
        appointment_id=1,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        price=300,
        business_timezone="Asia/Kolkata",
    )
    assert len(ids) == 2
    rows = (
        await pg_session.execute(
            text(
                "SELECT recipient_type, recipient_phone, idempotency_key "
                "FROM notification_outbox WHERE entity_id = 1 ORDER BY id"
            )
        )
    ).all()
    assert len(rows) == 2
    assert rows[0][0] == "patient"
    assert rows[1][0] == "owner"


async def test_two_owners_distinct_phones_produce_three_events(
    pg_session: AsyncSession,
) -> None:
    await _seed_clinic(pg_session, owner_phones=["+919000000001", "+919000000002"])
    svc = NotificationService(pg_session)
    ids = await svc.create_appointment_notifications(
        business_id=1,
        appointment_id=1,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        price=300,
        business_timezone="Asia/Kolkata",
    )
    assert len(ids) == 3
    rows = (
        await pg_session.execute(
            text(
                "SELECT recipient_type, recipient_phone "
                "FROM notification_outbox WHERE entity_id = 1 ORDER BY id"
            )
        )
    ).all()
    assert len(rows) == 3
    assert rows[0][0] == "patient"
    assert rows[1][0] == "owner"
    assert rows[1][1] == "+919000000001"
    assert rows[2][0] == "owner"
    assert rows[2][1] == "+919000000002"


async def test_inactive_owner_excluded_from_recipients(
    pg_session: AsyncSession,
) -> None:
    await _seed_clinic(pg_session, owner_phones=["+919000000001", "+919000000002"])
    await pg_session.execute(
        text("UPDATE business_users SET is_active = false WHERE phone = '+919000000002'")
    )
    await pg_session.flush()

    svc = NotificationService(pg_session)
    ids = await svc.create_appointment_notifications(
        business_id=1,
        appointment_id=1,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        price=300,
        business_timezone="Asia/Kolkata",
    )
    assert len(ids) == 2
    rows = (
        await pg_session.execute(
            text("SELECT recipient_type FROM notification_outbox WHERE entity_id = 1 ORDER BY id")
        )
    ).all()
    assert len(rows) == 2
    assert rows[0][0] == "patient"
    assert rows[1][0] == "owner"


async def test_owner_order_is_deterministic(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session, owner_phones=["+919000000002", "+919000000001"])
    svc = NotificationService(pg_session)
    await svc.create_appointment_notifications(
        business_id=1,
        appointment_id=1,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        price=300,
        business_timezone="Asia/Kolkata",
    )
    keys = (
        (
            await pg_session.execute(
                text(
                    "SELECT idempotency_key FROM notification_outbox "
                    "WHERE entity_id = 1 AND recipient_type = 'owner' ORDER BY id"
                )
            )
        )
        .scalars()
        .all()
    )
    assert keys[0] < keys[1]


# =========================================================================
# 2. ZERO OWNER FAIL-CLOSED
# =========================================================================


async def test_zero_owners_raises_configuration_error(
    pg_session: AsyncSession,
) -> None:
    await pg_session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental', 'clinic', '+919000000001', 'Asia/Kolkata', 'trial')"
        )
    )
    await pg_session.flush()

    svc = NotificationService(pg_session)
    with pytest.raises(NotificationConfigurationError) as exc_info:
        await svc.create_appointment_notifications(
            business_id=1,
            appointment_id=1,
            customer_phone="+919123456789",
            customer_name="Karthick",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            price=300,
            business_timezone="Asia/Kolkata",
        )
    assert exc_info.value.code == "no_valid_owner_recipients"

    count = await pg_session.scalar(text("SELECT count(*) FROM notification_outbox"))
    assert count == 0


# =========================================================================
# 3. IMMUTABLE EVIDENCE
# =========================================================================


async def test_events_contain_immutable_snapshot_and_digest(
    pg_session: AsyncSession,
) -> None:
    await _seed_clinic(pg_session)
    svc = NotificationService(pg_session)
    await svc.create_appointment_notifications(
        business_id=1,
        appointment_id=1,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        price=300,
        business_timezone="Asia/Kolkata",
    )

    rows = (
        (
            await pg_session.execute(
                text("SELECT payload FROM notification_outbox WHERE entity_id = 1 ORDER BY id")
            )
        )
        .scalars()
        .all()
    )

    for payload in rows:
        assert "equivalence_snapshot" in payload
        assert "equivalence_digest" in payload
        assert payload["schema_version"] == 1
        snapshot = payload["equivalence_snapshot"]
        assert snapshot["business_id"] == 1
        assert snapshot["appointment_id"] == 1


# =========================================================================
# 4. IDEMPOTENT CREATE
# =========================================================================


async def test_duplicate_create_returns_empty(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session)
    svc = NotificationService(pg_session)

    first = await svc.create_appointment_notifications(
        business_id=1,
        appointment_id=1,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        price=300,
        business_timezone="Asia/Kolkata",
    )
    assert len(first) == 2

    second = await svc.create_appointment_notifications(
        business_id=1,
        appointment_id=1,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        price=300,
        business_timezone="Asia/Kolkata",
    )
    assert len(second) == 0

    count = await pg_session.scalar(
        text("SELECT count(*) FROM notification_outbox WHERE entity_id = 1")
    )
    assert count == 2


# =========================================================================
# 5. REPLAY WITH CONFIG MUTATION
# =========================================================================


async def test_replay_after_config_change_returns_existing(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed_clinic(s1)
        svc = NotificationService(s1)
        original_ids = await svc.create_appointment_notifications(
            business_id=1,
            appointment_id=1,
            customer_phone="+919123456789",
            customer_name="Karthick",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            price=300,
            business_timezone="Asia/Kolkata",
        )
        assert len(original_ids) == 2
        await s1.commit()

    async with pg_session_factory() as s2:
        await s2.execute(text("UPDATE businesses SET name = 'New Dental Name' WHERE id = 1"))
        await s2.execute(
            text("UPDATE business_users SET phone = '+919999999999' WHERE business_id = 1")
        )
        await s2.commit()

    async with pg_session_factory() as s3:
        svc = NotificationService(s3)
        verify_ids = await svc.verify_appointment_notifications(business_id=1, appointment_id=1)
        assert set(verify_ids) == set(original_ids)

        count = await s3.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = 1")
        )
        assert count == 2


# =========================================================================
# 6. CORRUPTED EVIDENCE FAIL-CLOSED
# =========================================================================


async def test_corrupted_digest_fails_closed(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed_clinic(s1)
        svc = NotificationService(s1)
        await svc.create_appointment_notifications(
            business_id=1,
            appointment_id=1,
            customer_phone="+919123456789",
            customer_name="Karthick",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            price=300,
            business_timezone="Asia/Kolkata",
        )
        await s1.commit()

    async with pg_session_factory() as s2:
        await s2.execute(
            text(
                "UPDATE notification_outbox SET payload = "
                "jsonb_set(payload, '{equivalence_digest}', '\"corrupted\"') "
                "WHERE entity_id = 1 AND recipient_type = 'patient'"
            )
        )
        await s2.commit()

    async with pg_session_factory() as s3:
        svc = NotificationService(s3)
        with pytest.raises(NotificationEvidenceConflictError) as exc_info:
            await svc.verify_appointment_notifications(business_id=1, appointment_id=1)
        assert exc_info.value.code == "digest_mismatch"


async def test_corrupted_snapshot_fails_closed(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed_clinic(s1)
        svc = NotificationService(s1)
        await svc.create_appointment_notifications(
            business_id=1,
            appointment_id=1,
            customer_phone="+919123456789",
            customer_name="Karthick",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            price=300,
            business_timezone="Asia/Kolkata",
        )
        await s1.commit()

    async with pg_session_factory() as s2:
        await s2.execute(
            text(
                "UPDATE notification_outbox SET payload = "
                "jsonb_set(payload, '{equivalence_snapshot}', '\"not_a_dict\"') "
                "WHERE entity_id = 1 AND recipient_type = 'patient'"
            )
        )
        await s2.commit()

    async with pg_session_factory() as s3:
        svc = NotificationService(s3)
        with pytest.raises(NotificationEvidenceConflictError) as exc_info:
            await svc.verify_appointment_notifications(business_id=1, appointment_id=1)
        assert exc_info.value.code == "corrupted_snapshot"


async def test_missing_evidence_fails_closed(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed_clinic(s1)
        await s1.commit()

    async with pg_session_factory() as s2:
        svc = NotificationService(s2)
        with pytest.raises(NotificationEvidenceConflictError) as exc_info:
            await svc.verify_appointment_notifications(business_id=1, appointment_id=99)
        assert exc_info.value.code == "missing_evidence"


# =========================================================================
# 7. LEGACY EVIDENCE COMPATIBILITY
# =========================================================================


async def test_legacy_evidence_without_snapshot_is_accepted(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed_clinic(s1)
        await s1.execute(
            text(
                "INSERT INTO notification_outbox "
                "(business_id, event_type, entity_type, entity_id, "
                "recipient_type, recipient_phone, channel, payload, status, "
                "idempotency_key) VALUES "
                "(1, 'appointment_confirmed', 'appointment', 1, "
                "'patient', '+919123456789', 'whatsapp', "
                '\'{"clinic_name": "Smile", "appointment_id": 1, '
                '"phone_number_id": "phone-1"}\'::jsonb, '
                "'pending', 'legacy-patient-1')"
            )
        )
        await s1.commit()

    async with pg_session_factory() as s2:
        svc = NotificationService(s2)
        ids = await svc.verify_appointment_notifications(business_id=1, appointment_id=1)
        assert len(ids) == 1


# =========================================================================
# 8. ROLLBACK AND SESSION USABILITY
# =========================================================================


async def test_notification_rollback_leaves_no_partial_evidence(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        await session.commit()

    async with pg_session_factory() as session:
        svc = NotificationService(session)
        with pytest.raises(RuntimeError, match="force_rollback"):
            async with session.begin_nested():
                await svc.create_appointment_notifications(
                    business_id=1,
                    appointment_id=1,
                    customer_phone="+919123456789",
                    customer_name="Karthick",
                    service_name="Consultation",
                    resource_name="Dr. Priya",
                    start_at=NOW,
                    price=300,
                    business_timezone="Asia/Kolkata",
                )
                raise RuntimeError("force_rollback")

        count = await session.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = 1")
        )
        assert count == 0


# =========================================================================
# 9. CONCURRENT DUPLICATE CONFIRMATION (independent sessions)
# =========================================================================


async def test_concurrent_create_produces_exact_event_set(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await _seed_clinic(setup, owner_phones=["+919000000001", "+919000000002"])
        await setup.commit()

    barrier = asyncio.Event()

    async def create_notifications() -> list[int]:
        async with pg_session_factory() as session:
            await barrier.wait()
            svc = NotificationService(session)
            ids = await svc.create_appointment_notifications(
                business_id=1,
                appointment_id=1,
                customer_phone="+919123456789",
                customer_name="Karthick",
                service_name="Consultation",
                resource_name="Dr. Priya",
                start_at=NOW,
                price=300,
                business_timezone="Asia/Kolkata",
            )
            await session.commit()
            return ids

    t1 = asyncio.create_task(create_notifications())
    t2 = asyncio.create_task(create_notifications())
    barrier.set()
    r1, r2 = await asyncio.gather(t1, t2)

    all_ids = set(r1) | set(r2)
    assert len(all_ids) == 3

    async with pg_session_factory() as verify:
        count = await verify.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = 1")
        )
        assert count == 3


# =========================================================================
# 10. CANCELLATION EVENTS
# =========================================================================


async def test_cancellation_produces_events(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session)
    svc = NotificationService(pg_session)
    ids = await svc.create_cancellation_notifications(
        business_id=1,
        appointment_id=1,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        business_timezone="Asia/Kolkata",
        reason="patient_request",
    )
    assert len(ids) == 2
    types = (
        (
            await pg_session.execute(
                text("SELECT event_type FROM notification_outbox WHERE entity_id = 1 ORDER BY id")
            )
        )
        .scalars()
        .all()
    )
    assert all(t == "appointment_cancelled" for t in types)


# =========================================================================
# 11. RESCHEDULE EVENTS
# =========================================================================


async def test_reschedule_produces_events(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session)
    svc = NotificationService(pg_session)
    ids = await svc.create_reschedule_notifications(
        business_id=1,
        appointment_id=1,
        pending_action_id=10,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        old_start_at=NOW,
        new_start_at=NOW + timedelta(hours=2),
        business_timezone="Asia/Kolkata",
    )
    assert len(ids) == 2
    types = (
        (
            await pg_session.execute(
                text("SELECT event_type FROM notification_outbox WHERE entity_id = 1 ORDER BY id")
            )
        )
        .scalars()
        .all()
    )
    assert all(t == "appointment_rescheduled" for t in types)

    payload = (
        await pg_session.execute(
            text(
                "SELECT payload FROM notification_outbox "
                "WHERE entity_id = 1 AND recipient_type = 'patient'"
            )
        )
    ).scalar_one()
    assert "old_time" in payload
    assert "new_time" in payload
