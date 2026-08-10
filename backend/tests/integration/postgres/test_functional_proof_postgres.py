"""End-to-end functional proof: transactional notification foundation.

Proves the complete lifecycle:
1. Two active owners in a dental clinic
2. Appointment confirmation with transactional notifications
3. Immutable evidence in outbox (snapshot + digest per recipient)
4. Config mutation (clinic name, owner phone)
5. Replay returns exact existing evidence, zero new rows
6. Notification worker delivers without altering appointment state
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fonely.api.internal.validation import InternalValidationPort
from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCommand,
    CreatePendingAppointmentCommand,
)
from fonely.domain.appointments.results import PreCommitAppointmentSuccess
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.services.appointments import AppointmentService
from fonely.workers.notification_worker import (
    LoggingNotificationSender,
    run_notification_worker,
)

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import notifications, whatsapp_config

    mappings = '{"phone-1": 1}'
    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_phone_number_id", "phone-1")


async def _seed_two_owner_clinic(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental Clinic', 'dental', '+919000000001', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users (id, business_id, phone, role, is_active) VALUES "
            "(1, 1, '+919000000001', 'owner', true), "
            "(2, 1, '+919000000002', 'owner', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'General Consultation', 30, 0, 0, 500.00, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Dr. Priya', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) VALUES (1, 1, 1, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '09:00', '18:00', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


async def test_full_lifecycle_two_owners_config_mutation_replay_delivery(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # === STEP 1: Seed two-owner dental clinic ===
    async with pg_session_factory() as setup:
        await _seed_two_owner_clinic(setup)

    # === STEP 2: Confirm appointment with transactional notifications ===
    kolkata = ZoneInfo("Asia/Kolkata")
    target_day = datetime.now(kolkata).date() + timedelta(days=2)
    slot = datetime.combine(target_day, time(10, 30), tzinfo=kolkata).astimezone(UTC)

    actor = ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
    )

    async with pg_session_factory() as session:
        svc = AppointmentService(session, validation=InternalValidationPort(session))
        proposal = await svc.create_proposal(
            CreatePendingAppointmentCommand(
                actor=actor,
                service_id=1,
                resource_id=1,
                start_at=slot,
                customer_phone="+919123456789",
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                idempotency_key="proof-appt-1",
            )
        )
        await session.commit()

    async with pg_session_factory() as session:
        svc = AppointmentService(session, validation=InternalValidationPort(session))
        result = await svc.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=actor,
                pending_action_id=proposal.pending_action_id,
                expected_version=proposal.version,
            )
        )
        assert isinstance(result, PreCommitAppointmentSuccess)
        appointment_id = result.appointment.appointment_id
        await session.commit()

    # === STEP 3: Verify outbox evidence ===
    async with pg_session_factory() as verify:
        rows = (
            await verify.execute(
                text(
                    "SELECT recipient_type, recipient_phone, payload "
                    "FROM notification_outbox WHERE entity_id = :aid ORDER BY id"
                ),
                {"aid": appointment_id},
            )
        ).all()

        assert len(rows) == 3, f"Expected 3 events (1 patient + 2 owners), got {len(rows)}"

        patient = rows[0]
        assert patient[0] == "patient"
        assert patient[1] == "+919123456789"
        assert patient[2]["equivalence_snapshot"]["schema_version"] == 1
        assert patient[2]["equivalence_digest"]

        owner1 = rows[1]
        assert owner1[0] == "owner"
        assert owner1[1] == "+919000000001"
        assert owner1[2]["equivalence_snapshot"]["recipient_bu_id"] == 1

        owner2 = rows[2]
        assert owner2[0] == "owner"
        assert owner2[1] == "+919000000002"
        assert owner2[2]["equivalence_snapshot"]["recipient_bu_id"] == 2

        original_count = len(rows)

    # === STEP 4: Mutate clinic config ===
    async with pg_session_factory() as mutate:
        await mutate.execute(text("UPDATE businesses SET name = 'Totally New Dental' WHERE id = 1"))
        await mutate.execute(text("UPDATE business_users SET phone = '+919888888888' WHERE id = 1"))
        await mutate.commit()

    # === STEP 5: Replay — returns exact existing, zero new rows ===
    async with pg_session_factory() as replay:
        svc = AppointmentService(replay, validation=InternalValidationPort(replay))
        replay_result = await svc.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=actor,
                pending_action_id=proposal.pending_action_id,
                expected_version=999,
            )
        )
        assert isinstance(replay_result, PreCommitAppointmentSuccess)
        assert replay_result.appointment.appointment_id == appointment_id
        await replay.commit()

    async with pg_session_factory() as verify2:
        new_count = await verify2.scalar(
            text("SELECT count(*) FROM notification_outbox WHERE entity_id = :aid"),
            {"aid": appointment_id},
        )
        assert new_count == original_count, (
            f"Replay created {new_count - original_count} new outbox rows"
        )

        appt_count = await verify2.scalar(
            text("SELECT count(*) FROM appointments WHERE id = :aid"),
            {"aid": appointment_id},
        )
        assert appt_count == 1

        alloc_count = await verify2.scalar(
            text(
                "SELECT count(*) FROM resource_allocations "
                "WHERE appointment_id = :aid AND status = 'active'"
            ),
            {"aid": appointment_id},
        )
        assert alloc_count == 1

    # === STEP 6: Notification worker delivery ===
    sender = LoggingNotificationSender()
    await run_notification_worker(pg_session_factory, sender, max_iterations=2, batch_size=10)

    async with pg_session_factory() as final:
        delivered = (
            (
                await final.execute(
                    text(
                        "SELECT status FROM notification_outbox WHERE entity_id = :aid ORDER BY id"
                    ),
                    {"aid": appointment_id},
                )
            )
            .scalars()
            .all()
        )

        assert all(s == "delivered" for s in delivered), f"Statuses: {delivered}"

        appt_status = await final.scalar(
            text("SELECT status FROM appointments WHERE id = :aid"),
            {"aid": appointment_id},
        )
        assert appt_status == "confirmed", "Delivery must not alter appointment state"
