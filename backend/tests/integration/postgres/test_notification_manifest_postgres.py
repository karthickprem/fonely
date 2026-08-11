"""PostgreSQL evidence for notification manifests.

Covers: multi-owner, zero-owner, atomicity, manifest verification,
config mutation replay, corrupted evidence, legacy compatibility,
reschedule operations, tenant isolation, and retention.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.services.notifications import (
    NotificationConfigurationError,
    NotificationEvidenceConflictError,
    NotificationService,
)
from tests.integration.postgres.conftest import seed_whatsapp_channel

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 15, 4, 30, tzinfo=UTC)


async def _seed(
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
    await seed_whatsapp_channel(session)
    for i, phone in enumerate(owner_phones, start=1):
        await session.execute(
            text(
                "INSERT INTO business_users (id, business_id, phone, role, is_active) "
                "VALUES (:uid, :bid, :phone, 'owner', true)"
            ),
            {"uid": business_id * 100 + i, "bid": business_id, "phone": phone},
        )
    await session.flush()


async def _seed_pa(session: AsyncSession, pa_id: int = 1, business_id: int = 1) -> None:
    await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "status, expires_at, idempotency_key, version, payload_digest) VALUES "
            "(:id, :bid, 'appointment', 1, '{}'::jsonb, 'confirmed', "
            "now() + interval '1 hour', :key, 3, "
            "'aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff6666aaaa7777bbbb8888')"
        ),
        {"id": pa_id, "bid": business_id, "key": f"pa-manifest-{pa_id}"},
    )


async def _create_notifs(
    session: AsyncSession,
    appt_id: int = 1,
    pa_id: int = 1,
) -> list[int]:
    svc = NotificationService(session)
    return await svc.create_appointment_notifications(
        business_id=1,
        appointment_id=appt_id,
        pending_action_id=pa_id,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        price=300,
        business_timezone="Asia/Kolkata",
        actor_kind="customer",
        actor_phone="+919123456789",
    )


# === MULTI-OWNER ===


async def test_two_owners_produce_three_events_and_manifest(
    pg_session: AsyncSession,
) -> None:
    await _seed(pg_session, owner_phones=["+919000000001", "+919000000002"])
    await _seed_pa(pg_session)
    ids = await _create_notifs(pg_session)
    assert len(ids) == 3

    manifest = (
        await pg_session.execute(
            text(
                "SELECT recipient_count, recipient_manifest, equivalence_digest "
                "FROM notification_manifests WHERE business_id = 1"
            )
        )
    ).one()
    assert manifest[0] == 3
    entries = manifest[1]
    assert len(entries) == 3
    assert entries[0]["recipient_type"] == "patient"
    assert entries[1]["recipient_type"] == "owner"
    assert entries[1]["bu_id"] == 101
    assert entries[2]["recipient_type"] == "owner"
    assert entries[2]["bu_id"] == 102
    assert manifest[2]


# === ZERO OWNER ===


async def test_zero_owners_fails_before_mutation(pg_session: AsyncSession) -> None:
    await pg_session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile', 'clinic', '+919000000001', 'Asia/Kolkata', 'trial')"
        )
    )
    await _seed_pa(pg_session)
    await pg_session.flush()

    with pytest.raises(NotificationConfigurationError) as exc_info:
        await _create_notifs(pg_session)
    assert exc_info.value.code == "no_valid_owner_recipients"

    count = await pg_session.scalar(text("SELECT count(*) FROM notification_outbox"))
    assert count == 0
    manifest_count = await pg_session.scalar(text("SELECT count(*) FROM notification_manifests"))
    assert manifest_count == 0


# === MANIFEST VERIFICATION ===


async def test_verify_returns_verified_with_manifest(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed(s1)
        await _seed_pa(s1)
        ids = await _create_notifs(s1)
        await s1.commit()

    async with pg_session_factory() as s2:
        svc = NotificationService(s2)
        evidence = await svc.verify_appointment_notifications(
            business_id=1, appointment_id=1, pending_action_id=1
        )
        assert evidence.notification_evidence == "verified"
        assert evidence.appointment_result_authoritative is True
        assert set(evidence.event_ids) == set(ids)


# === CONFIG MUTATION REPLAY ===


async def test_replay_after_config_change(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed(s1)
        await _seed_pa(s1)
        original = await _create_notifs(s1)
        await s1.commit()

    async with pg_session_factory() as s2:
        await s2.execute(text("UPDATE businesses SET name = 'New Name' WHERE id = 1"))
        await s2.execute(
            text("UPDATE business_users SET phone = '+919999999999' WHERE business_id = 1")
        )
        await s2.commit()

    async with pg_session_factory() as s3:
        svc = NotificationService(s3)
        evidence = await svc.verify_appointment_notifications(
            business_id=1, appointment_id=1, pending_action_id=1
        )
        assert evidence.notification_evidence == "verified"
        assert set(evidence.event_ids) == set(original)

        count = await s3.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = 1")
        )
        assert count == 2


# === CORRUPTED EVIDENCE ===


async def test_corrupted_manifest_digest_fails_closed(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed(s1)
        await _seed_pa(s1)
        await _create_notifs(s1)
        await s1.commit()

    async with pg_session_factory() as s2:
        await s2.execute(
            text(
                "UPDATE notification_manifests "
                "SET equivalence_digest = 'corrupted' WHERE business_id = 1"
            )
        )
        await s2.commit()

    async with pg_session_factory() as s3:
        svc = NotificationService(s3)
        with pytest.raises(NotificationEvidenceConflictError) as exc_info:
            await svc.verify_appointment_notifications(
                business_id=1, appointment_id=1, pending_action_id=1
            )
        assert exc_info.value.code == "manifest_corrupted"


# === LEGACY EVIDENCE ===


async def test_legacy_outbox_without_manifest_returns_unverifiable(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed(s1)
        await s1.execute(
            text(
                "INSERT INTO notification_outbox "
                "(business_id, event_type, entity_type, entity_id, "
                "recipient_type, recipient_phone, channel, payload, status, "
                "idempotency_key) VALUES "
                "(1, 'appointment_confirmed', 'appointment', 1, "
                "'patient', '+919123456789', 'whatsapp', "
                '\'{"clinic_name": "Smile", "appointment_id": 1}\'::jsonb, '
                "'delivered', 'legacy-patient-1')"
            )
        )
        await s1.commit()

    async with pg_session_factory() as s2:
        svc = NotificationService(s2)
        evidence = await svc.verify_appointment_notifications(business_id=1, appointment_id=1)
        assert evidence.notification_evidence == "unverifiable"
        assert evidence.appointment_result_authoritative is True


async def test_no_evidence_returns_irrecoverable(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed(s1)
        await s1.commit()

    async with pg_session_factory() as s2:
        svc = NotificationService(s2)
        evidence = await svc.verify_appointment_notifications(business_id=1, appointment_id=99)
        assert evidence.notification_evidence == "irrecoverable"
        assert evidence.appointment_result_authoritative is True


# === RETENTION ===


async def test_manifest_survives_outbox_deletion(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as s1:
        await _seed(s1)
        await _seed_pa(s1)
        await _create_notifs(s1)
        await s1.commit()

    async with pg_session_factory() as s2:
        await s2.execute(text("DELETE FROM notification_outbox WHERE entity_id = 1"))
        await s2.commit()

    async with pg_session_factory() as s3:
        svc = NotificationService(s3)
        evidence = await svc.verify_appointment_notifications(
            business_id=1, appointment_id=1, pending_action_id=1
        )
        assert evidence.notification_evidence == "verified_delivery_unknown"
        assert len(evidence.event_ids) == 2

        outbox = await s3.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = 1")
        )
        assert outbox == 0


# === ROLLBACK ATOMICITY ===


async def test_savepoint_rollback_removes_manifest_and_outbox(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed(session)
        await _seed_pa(session)
        await session.commit()

    async with pg_session_factory() as session:
        svc = NotificationService(session)
        with pytest.raises(RuntimeError, match="force_rollback"):
            async with session.begin_nested():
                await svc.create_appointment_notifications(
                    business_id=1,
                    appointment_id=1,
                    pending_action_id=1,
                    customer_phone="+919123456789",
                    customer_name="K",
                    service_name="C",
                    resource_name="D",
                    start_at=NOW,
                    price=100,
                    business_timezone="Asia/Kolkata",
                    actor_kind="customer",
                    actor_phone="+919123456789",
                )
                raise RuntimeError("force_rollback")

        outbox = await session.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = 1")
        )
        assert outbox == 0
        manifest = await session.scalar(
            text("SELECT count(*) FROM notification_manifests WHERE entity_id = 1")
        )
        assert manifest == 0


# === CANCELLATION ===


async def test_cancellation_creates_manifest(pg_session: AsyncSession) -> None:
    await _seed(pg_session)
    await _seed_pa(pg_session, pa_id=10)
    svc = NotificationService(pg_session)
    ids = await svc.create_cancellation_notifications(
        business_id=1,
        appointment_id=1,
        pending_action_id=10,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=NOW,
        business_timezone="Asia/Kolkata",
        reason="patient_request",
        actor_kind="customer",
        actor_phone="+919123456789",
    )
    assert len(ids) == 2

    manifest = (
        await pg_session.execute(
            text(
                "SELECT operation, recipient_count FROM notification_manifests "
                "WHERE pending_action_id = 10"
            )
        )
    ).one()
    assert manifest[0] == "cancel"
    assert manifest[1] == 2


# === RESCHEDULE ===


async def test_reschedule_creates_manifest_with_old_new_times(
    pg_session: AsyncSession,
) -> None:
    await _seed(pg_session)
    await _seed_pa(pg_session, pa_id=20)
    svc = NotificationService(pg_session)
    ids = await svc.create_reschedule_notifications(
        business_id=1,
        appointment_id=1,
        pending_action_id=20,
        customer_phone="+919123456789",
        customer_name="Karthick",
        service_name="Consultation",
        resource_name="Dr. Priya",
        old_start_at=NOW,
        new_start_at=NOW + timedelta(hours=1),
        business_timezone="Asia/Kolkata",
        actor_kind="customer",
        actor_phone="+919123456789",
    )
    assert len(ids) == 2

    manifest = (
        await pg_session.execute(
            text(
                "SELECT operation, recipient_manifest FROM notification_manifests "
                "WHERE pending_action_id = 20"
            )
        )
    ).one()
    assert manifest[0] == "reschedule"
    patient_snapshot = manifest[1][0]["snapshot"]
    assert patient_snapshot["old_start_at"] is not None
    assert patient_snapshot["new_start_at"] is not None
    assert patient_snapshot["old_start_at"] != patient_snapshot["new_start_at"]


async def test_two_reschedules_produce_distinct_manifests(
    pg_session: AsyncSession,
) -> None:
    await _seed(pg_session)
    await _seed_pa(pg_session, pa_id=30)
    await _seed_pa(pg_session, pa_id=31)

    svc = NotificationService(pg_session)
    ids1 = await svc.create_reschedule_notifications(
        business_id=1,
        appointment_id=1,
        pending_action_id=30,
        customer_phone="+919123456789",
        customer_name="K",
        service_name="C",
        resource_name="D",
        old_start_at=NOW,
        new_start_at=NOW + timedelta(hours=1),
        business_timezone="Asia/Kolkata",
        actor_kind="customer",
        actor_phone="+919123456789",
    )
    ids2 = await svc.create_reschedule_notifications(
        business_id=1,
        appointment_id=1,
        pending_action_id=31,
        customer_phone="+919123456789",
        customer_name="K",
        service_name="C",
        resource_name="D",
        old_start_at=NOW + timedelta(hours=1),
        new_start_at=NOW + timedelta(hours=2),
        business_timezone="Asia/Kolkata",
        actor_kind="customer",
        actor_phone="+919123456789",
    )
    assert len(ids1) == 2
    assert len(ids2) == 2

    manifest_count = await pg_session.scalar(
        text("SELECT count(*) FROM notification_manifests WHERE entity_id = 1")
    )
    assert manifest_count == 2


# === TENANT ISOLATION ===


async def test_cross_tenant_manifest_isolation(
    pg_session: AsyncSession,
) -> None:
    await _seed(pg_session, business_id=1)
    await _seed(pg_session, business_id=2)
    await _seed_pa(pg_session, pa_id=1, business_id=1)
    await _seed_pa(pg_session, pa_id=2, business_id=2)

    svc = NotificationService(pg_session)
    await svc.create_appointment_notifications(
        business_id=1,
        appointment_id=1,
        pending_action_id=1,
        customer_phone="+919123456789",
        customer_name="K",
        service_name="C",
        resource_name="D",
        start_at=NOW,
        price=100,
        business_timezone="Asia/Kolkata",
        actor_kind="customer",
        actor_phone="+919123456789",
    )

    evidence_b1 = await svc.verify_appointment_notifications(
        business_id=1, appointment_id=1, pending_action_id=1
    )
    assert evidence_b1.notification_evidence == "verified"

    evidence_b2 = await svc.verify_appointment_notifications(business_id=2, appointment_id=1)
    assert evidence_b2.notification_evidence == "irrecoverable"
