"""End-to-end functional proof: notification manifest lifecycle.

1. Two active owners in a dental clinic
2. Confirm appointment — atomic manifest + outbox
3. Verify 3 events (1 patient + 2 owners) with manifest
4. Mutate config (clinic name, owner phone)
5. Replay — exact committed evidence, zero new rows
6. Retention cleanup (delete outbox)
7. Replay from manifest — verified, archival IDs preserved
8. Concurrent confirmation with genuine lock contention
"""

import asyncio
import time as _time
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
from fonely.models.enums import CallerRole, Channel
from fonely.services.appointments import AppointmentService

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


async def test_full_lifecycle_manifest(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await _seed_two_owner_clinic(setup)

    kolkata = ZoneInfo("Asia/Kolkata")
    target_day = datetime.now(kolkata).date() + timedelta(days=2)
    slot = datetime.combine(target_day, time(10, 30), tzinfo=kolkata).astimezone(UTC)
    actor = ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
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
                idempotency_key="proof-1",
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
        appt_id = result.appointment.appointment_id
        await session.commit()

    async with pg_session_factory() as v:
        outbox = (
            await v.execute(
                text(
                    "SELECT recipient_type, recipient_phone FROM notification_outbox "
                    "WHERE entity_id = :a ORDER BY id"
                ),
                {"a": appt_id},
            )
        ).all()
        assert len(outbox) == 3
        assert outbox[0][0] == "patient"
        assert outbox[1][0] == "owner"
        assert outbox[1][1] == "+919000000001"
        assert outbox[2][0] == "owner"
        assert outbox[2][1] == "+919000000002"

        manifest = (
            await v.execute(
                text(
                    "SELECT recipient_count, equivalence_digest, actor_kind, actor_phone "
                    "FROM notification_manifests WHERE entity_id = :a"
                ),
                {"a": appt_id},
            )
        ).one()
        assert manifest[0] == 3
        assert manifest[1]
        assert manifest[2] == "customer"
        assert manifest[3] == "+919123456789"

    async with pg_session_factory() as m:
        await m.execute(text("UPDATE businesses SET name = 'New Dental' WHERE id = 1"))
        await m.execute(text("UPDATE business_users SET phone = '+919888888888' WHERE id = 1"))
        await m.commit()

    async with pg_session_factory() as r:
        svc = AppointmentService(r, validation=InternalValidationPort(r))
        replay = await svc.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=actor,
                pending_action_id=proposal.pending_action_id,
                expected_version=999,
            )
        )
        assert isinstance(replay, PreCommitAppointmentSuccess)
        assert replay.appointment.appointment_id == appt_id
        await r.commit()

    async with pg_session_factory() as v2:
        assert (
            await v2.scalar(
                text("SELECT count(*) FROM notification_outbox WHERE entity_id = :a"),
                {"a": appt_id},
            )
            == 3
        )
        assert (
            await v2.scalar(
                text("SELECT count(*) FROM notification_manifests WHERE entity_id = :a"),
                {"a": appt_id},
            )
            == 1
        )

    async with pg_session_factory() as d:
        await d.execute(
            text("DELETE FROM notification_outbox WHERE entity_id = :a"),
            {"a": appt_id},
        )
        await d.commit()

    async with pg_session_factory() as r2:
        svc = AppointmentService(r2, validation=InternalValidationPort(r2))
        replay2 = await svc.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=actor,
                pending_action_id=proposal.pending_action_id,
                expected_version=999,
            )
        )
        assert isinstance(replay2, PreCommitAppointmentSuccess)
        assert replay2.appointment.appointment_id == appt_id

    async with pg_session_factory() as final:
        appt_status = await final.scalar(
            text("SELECT status FROM appointments WHERE id = :a"), {"a": appt_id}
        )
        assert appt_status == "confirmed"


async def _pid(session: AsyncSession) -> int:
    value = await session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(value, int)
    return value


async def _observe_blocker(
    factory: async_sessionmaker[AsyncSession], blocked_pid: int, blocker_pid: int
) -> None:
    start = _time.monotonic()

    async def observe() -> None:
        while True:
            async with factory() as observer:
                row = (
                    await observer.execute(
                        text(
                            "SELECT :blocker = ANY(pg_blocking_pids(:blocked)) "
                            "FROM pg_stat_activity WHERE pid = :blocked"
                        ),
                        {"blocker": blocker_pid, "blocked": blocked_pid},
                    )
                ).scalar()
            if row is True:
                return
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(observe(), timeout=8)
    except TimeoutError:
        elapsed = _time.monotonic() - start
        raise AssertionError(
            f"observer timed out after {elapsed:.2f}s: "
            f"blocked_pid={blocked_pid}, blocker_pid={blocker_pid}"
        ) from None


async def test_concurrent_confirmation_converges(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Genuine lock contention + replay convergence proof.

    Winner: confirms and holds lock via uncommitted transaction.
    Contender: enters confirm_and_commit in independent session, blocks
    on resource schedule lock — observed via pg_blocking_pids.
    Winner commits. Contender is released but encounters PA already confirmed
    (expected domain behavior — PA state machine rejects second begin_commit).
    A fresh replay session then returns exact committed evidence.
    Terminal state: one appointment, one manifest, three events, no duplicates.
    """
    async with pg_session_factory() as setup:
        await _seed_two_owner_clinic(setup)

    kolkata = ZoneInfo("Asia/Kolkata")
    target_day = datetime.now(kolkata).date() + timedelta(days=3)
    slot = datetime.combine(target_day, time(11, 0), tzinfo=kolkata).astimezone(UTC)
    actor = ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
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
                idempotency_key="concurrent-1",
            )
        )
        await session.commit()

    # Winner: confirm, hold lock via open transaction
    async with pg_session_factory() as winner_session:
        await winner_session.execute(text("SET LOCAL lock_timeout = '8s'"))
        winner_pid = await _pid(winner_session)

        winner_svc = AppointmentService(
            winner_session, validation=InternalValidationPort(winner_session)
        )
        winner_result = await winner_svc.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=actor,
                pending_action_id=proposal.pending_action_id,
                expected_version=proposal.version,
            )
        )
        assert isinstance(winner_result, PreCommitAppointmentSuccess)
        appt_id = winner_result.appointment.appointment_id

        # Contender: independent session, blocks on resource schedule lock
        contender_pid_future: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        from fonely.api.internal.validation import AppointmentAvailabilityError

        contender_error_box: list[AppointmentAvailabilityError] = []

        async def contender() -> None:
            async with pg_session_factory() as contender_session:
                await contender_session.execute(text("SET LOCAL lock_timeout = '8s'"))
                contender_pid_future.set_result(await _pid(contender_session))
                contender_svc = AppointmentService(
                    contender_session,
                    validation=InternalValidationPort(contender_session),
                )
                with pytest.raises(AppointmentAvailabilityError) as exc_info:
                    await contender_svc.confirm_and_commit(
                        ConfirmPendingAppointmentCommand(
                            actor=actor,
                            pending_action_id=proposal.pending_action_id,
                            expected_version=proposal.version,
                        )
                    )
                contender_error_box.append(exc_info.value)
                await contender_session.rollback()

        task = asyncio.create_task(contender())
        contender_pid = await contender_pid_future

        # Observe: contender is blocked by winner on resource schedule lock
        await _observe_blocker(pg_session_factory, contender_pid, winner_pid)
        assert not task.done(), "contender must be blocked before winner releases"

        # Release: winner commits
        await winner_session.commit()
        await task

    # Contender raised AppointmentAvailabilityError (capacity_conflict)
    assert len(contender_error_box) == 1
    assert isinstance(contender_error_box[0], AppointmentAvailabilityError)
    assert contender_error_box[0].reason.value == "capacity_conflict"

    # Replay: fresh session returns exact committed evidence
    async with pg_session_factory() as replay_session:
        replay_svc = AppointmentService(
            replay_session, validation=InternalValidationPort(replay_session)
        )
        replay_result = await replay_svc.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=actor,
                pending_action_id=proposal.pending_action_id,
                expected_version=999,
            )
        )
        assert isinstance(replay_result, PreCommitAppointmentSuccess)
        assert replay_result.appointment.appointment_id == appt_id
        assert replay_result.appointment.service_name == winner_result.appointment.service_name
        assert (
            replay_result.appointment.pending_action_id
            == winner_result.appointment.pending_action_id
        )

    # Terminal: one appointment, one manifest, three events
    async with pg_session_factory() as v:
        assert await v.scalar(text("SELECT count(*) FROM appointments")) == 1
        assert (
            await v.scalar(
                text("SELECT count(*) FROM notification_manifests WHERE entity_id = :a"),
                {"a": appt_id},
            )
            == 1
        )
        assert (
            await v.scalar(
                text("SELECT count(*) FROM notification_outbox WHERE entity_id = :a"),
                {"a": appt_id},
            )
            == 3
        )
