"""Independent-session PostgreSQL evidence for v1 notification pair repair races.

Two sessions observe the same incomplete v1 pair (one member missing), then both
attempt repair concurrently. PostgreSQL's unique constraint + ON CONFLICT DO NOTHING
must converge to exactly one new row.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.services.notifications import NotificationService

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 12, 13, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import notifications, whatsapp_config

    mappings = '{"phone-1": 1}'
    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_phone_number_id", "phone-1")


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


async def _create_v1_pair(
    session: AsyncSession,
    *,
    appointment_id: int = 42,
    operation: str = "create",
) -> list[int]:
    service = NotificationService(session)
    if operation == "create":
        return await service.create_appointment_notifications(
            business_id=1,
            appointment_id=appointment_id,
            customer_phone="+919123456789",
            customer_name="Patient",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            price=300,
            business_timezone="Asia/Kolkata",
        )
    if operation == "cancel":
        return await service.create_cancellation_notifications(
            business_id=1,
            appointment_id=appointment_id,
            customer_phone="+919123456789",
            customer_name="Patient",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            business_timezone="Asia/Kolkata",
            reason="Requested",
        )
    return await service.create_reschedule_notifications(
        business_id=1,
        appointment_id=appointment_id,
        pending_action_id=44,
        customer_phone="+919123456789",
        customer_name="Patient",
        service_name="Consultation",
        resource_name="Dr. Priya",
        old_start_at=NOW,
        new_start_at=NOW + timedelta(hours=2),
        business_timezone="Asia/Kolkata",
    )


async def _verify_pair(
    session: AsyncSession,
    *,
    appointment_id: int = 42,
    operation: str = "create",
) -> list[int]:
    service = NotificationService(session)
    if operation == "create":
        return await service.verify_appointment_notifications(
            business_id=1,
            appointment_id=appointment_id,
            customer_phone="+919123456789",
            customer_name="Patient",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            price=300,
            business_timezone="Asia/Kolkata",
        )
    if operation == "cancel":
        return await service.verify_cancellation_notifications(
            business_id=1,
            appointment_id=appointment_id,
            customer_phone="+919123456789",
            customer_name="Patient",
            service_name="Consultation",
            resource_name="Dr. Priya",
            start_at=NOW,
            business_timezone="Asia/Kolkata",
            reason="Requested",
        )
    return await service.verify_reschedule_notifications(
        business_id=1,
        appointment_id=appointment_id,
        pending_action_id=44,
        customer_phone="+919123456789",
        customer_name="Patient",
        service_name="Consultation",
        resource_name="Dr. Priya",
        old_start_at=NOW,
        new_start_at=NOW + timedelta(hours=2),
        business_timezone="Asia/Kolkata",
    )


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
@pytest.mark.parametrize("missing", ["patient", "owner"])
async def test_concurrent_repair_converges_to_one_row(
    pg_session_factory: async_sessionmaker[AsyncSession],
    operation: str,
    missing: str,
) -> None:
    """Two independent sessions race to repair the same missing v1 member.

    Exactly one new row must be created. Both sessions must return the same
    ordered pair of IDs. The surviving member must be unchanged.
    """
    appt_id = 42

    async with pg_session_factory() as setup:
        await _seed_clinic(setup)
        original_ids = await _create_v1_pair(setup, appointment_id=appt_id, operation=operation)
        await setup.commit()

    assert len(original_ids) == 2
    surviving_idx = 1 if missing == "patient" else 0
    surviving_id = original_ids[surviving_idx]

    async with pg_session_factory() as delete_session:
        delete_idx = 0 if missing == "patient" else 1
        deleted_id = original_ids[delete_idx]
        await delete_session.execute(
            text("DELETE FROM notification_outbox WHERE id = :id"),
            {"id": deleted_id},
        )
        await delete_session.commit()

    async with pg_session_factory() as verify_incomplete:
        count = await verify_incomplete.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = :eid"),
            {"eid": appt_id},
        )
        assert count == 1

    both_observed = asyncio.Event()
    observed_count = 0
    observed_lock = asyncio.Lock()
    results: list[list[int] | Exception] = [[], []]

    async def racer(idx: int) -> None:
        nonlocal observed_count
        async with pg_session_factory() as session:
            # Verify this session sees the incomplete pair before proceeding
            count = await session.scalar(
                text("SELECT count(*) FROM notification_outbox WHERE entity_id = :eid"),
                {"eid": appt_id},
            )
            assert count == 1, f"racer {idx} sees {count} rows, expected 1"
            # Signal readiness after confirming incomplete state
            async with observed_lock:
                observed_count += 1
                if observed_count == 2:
                    both_observed.set()
            await asyncio.wait_for(both_observed.wait(), timeout=5.0)
            try:
                ids = await _verify_pair(session, appointment_id=appt_id, operation=operation)
                await session.commit()
                results[idx] = ids
            except Exception as exc:
                results[idx] = exc  # type: ignore[assignment]

    await asyncio.gather(racer(0), racer(1))

    for i, result in enumerate(results):
        assert not isinstance(result, Exception), f"racer {i} failed: {result}"
    ids_0 = results[0]
    ids_1 = results[1]
    assert isinstance(ids_0, list) and isinstance(ids_1, list)

    assert len(ids_0) == 2
    assert len(ids_1) == 2
    assert ids_0 == ids_1

    assert surviving_id in ids_0

    async with pg_session_factory() as final:
        final_count = await final.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = :eid"),
            {"eid": appt_id},
        )
        assert final_count == 2

        patient_count = await final.scalar(
            text(
                "SELECT count(*) FROM notification_outbox "
                "WHERE entity_id = :eid AND recipient_type = 'patient'"
            ),
            {"eid": appt_id},
        )
        owner_count = await final.scalar(
            text(
                "SELECT count(*) FROM notification_outbox "
                "WHERE entity_id = :eid AND recipient_type = 'owner'"
            ),
            {"eid": appt_id},
        )
        assert patient_count == 1
        assert owner_count == 1


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_third_replay_after_repair_is_noop(
    pg_session_factory: async_sessionmaker[AsyncSession],
    operation: str,
) -> None:
    """After concurrent repair converges, a third replay creates no new rows."""
    appt_id = 43

    async with pg_session_factory() as setup:
        await _seed_clinic(setup)
        original_ids = await _create_v1_pair(setup, appointment_id=appt_id, operation=operation)
        await setup.commit()

    async with pg_session_factory() as delete_session:
        await delete_session.execute(
            text("DELETE FROM notification_outbox WHERE id = :id"),
            {"id": original_ids[1]},
        )
        await delete_session.commit()

    async with pg_session_factory() as repair:
        repaired = await _verify_pair(repair, appointment_id=appt_id, operation=operation)
        await repair.commit()

    async with pg_session_factory() as third:
        third_ids = await _verify_pair(third, appointment_id=appt_id, operation=operation)
        count = await third.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = :eid"),
            {"eid": appt_id},
        )
        assert count == 2
        assert third_ids == repaired


@pytest.mark.parametrize("operation", ["create", "cancel", "reschedule"])
async def test_savepoint_rollback_on_second_insert_failure(
    pg_session_factory: async_sessionmaker[AsyncSession],
    operation: str,
) -> None:
    """If the second pair member insert fails, savepoint rolls back the first."""
    appt_id = 44

    async with pg_session_factory() as setup:
        await _seed_clinic(setup)
        await setup.commit()

    async with pg_session_factory() as session:
        sentinel_result = await session.execute(
            text(
                "INSERT INTO notification_outbox "
                "(business_id, event_type, entity_type, entity_id, "
                "recipient_type, recipient_phone, channel, payload, "
                "status, idempotency_key) "
                "VALUES (1, 'appointment_confirmed', 'sentinel', 999, "
                "'patient', '+910000000000', 'whatsapp', "
                "'{}'::jsonb, 'pending', 'sentinel-key') RETURNING id"
            )
        )
        sentinel_id = sentinel_result.scalar_one()
        await session.flush()

        from fonely.repositories.notifications import NotificationRepository

        real_repo = NotificationRepository(session)
        original_insert = real_repo.insert_event_idempotent
        call_count = 0

        async def failing_second_insert(values):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("injected_second_insert_failure")
            return await original_insert(values)

        real_repo.insert_event_idempotent = failing_second_insert  # type: ignore[assignment]

        service = NotificationService(session)
        service._repo = real_repo

        with pytest.raises(RuntimeError, match="injected_second_insert_failure"):
            await service.create_appointment_notifications(
                business_id=1,
                appointment_id=appt_id,
                customer_phone="+919123456789",
                customer_name="Patient",
                service_name="Consultation",
                resource_name="Dr. Priya",
                start_at=NOW,
                price=300,
                business_timezone="Asia/Kolkata",
            )

        pair_count = await session.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = :eid"),
            {"eid": appt_id},
        )
        assert pair_count == 0

        sentinel_exists = await session.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE id = :id"),
            {"id": sentinel_id},
        )
        assert sentinel_exists == 1

        await session.commit()

    async with pg_session_factory() as verify:
        committed_pair = await verify.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = :eid"),
            {"eid": appt_id},
        )
        assert committed_pair == 0

        sentinel_committed = await verify.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE id = :id"),
            {"id": sentinel_id},
        )
        assert sentinel_committed == 1
