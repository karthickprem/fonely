"""Observed-lock PostgreSQL evidence for schedule mutations versus confirmation."""

import asyncio
import json
import time as _time
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.api.internal.validation import InternalValidationPort
from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCommand,
    CreatePendingAppointmentCommand,
)
from fonely.domain.appointments.results import PreCommitAppointmentSuccess
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.services.appointments import AppointmentService
from fonely.services.model_gateway import ModelResponse
from fonely.services.owner_commands import OwnerCommandService
from tests.integration.postgres.conftest import seed_whatsapp_channel

pytestmark = pytest.mark.postgres


KOLKATA = ZoneInfo("Asia/Kolkata")


def _customer() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
    )


def _target() -> tuple[datetime, str]:
    day = datetime.now(KOLKATA).date() + timedelta(days=2)
    start = datetime.combine(day, time(17, 30), tzinfo=KOLKATA).astimezone(UTC)
    return start, day.isoformat()


def _gateway(command: str, target_date: str) -> AsyncMock:
    payload: dict[str, object] = {"command": command, "date": target_date}
    if command == "doctor_leave":
        payload["doctor_name"] = "Dr. Priya"
    if command == "close_early":
        payload["close_time"] = "17:00"
    gateway = AsyncMock()
    gateway.complete.return_value = ModelResponse(text=json.dumps(payload))
    return gateway


async def _timeouts(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL lock_timeout = '8s'"))
    await session.execute(text("SET LOCAL statement_timeout = '15s'"))
    await session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '15s'"))


async def _pid(session: AsyncSession) -> int:
    value = await session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(value, int)
    return value


async def _observe_blocker(
    factory: async_sessionmaker[AsyncSession], blocked_pid: int, blocker_pid: int
) -> None:
    last_observed: tuple[object, ...] | None = None
    start = _time.monotonic()

    async def observe() -> None:
        nonlocal last_observed
        while True:
            async with factory() as observer:
                row = (
                    await observer.execute(
                        text(
                            "SELECT :blocker = ANY(pg_blocking_pids(:blocked)), "
                            "wait_event_type FROM pg_stat_activity WHERE pid = :blocked"
                        ),
                        {"blocker": blocker_pid, "blocked": blocked_pid},
                    )
                ).one_or_none()
            last_observed = tuple(row) if row is not None else None
            if row is not None and row[0] is True:
                assert row[1] == "Lock", (
                    f"blocker observed but wait_event_type={row[1]!r} "
                    f"(expected 'Lock'), blocked_pid={blocked_pid}, "
                    f"blocker_pid={blocker_pid}, "
                    f"elapsed={_time.monotonic() - start:.2f}s"
                )
                return
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(observe(), timeout=5)
    except TimeoutError:
        elapsed = _time.monotonic() - start
        raise AssertionError(
            f"observer timed out after {elapsed:.2f}s waiting for blocker: "
            f"blocked_pid={blocked_pid}, blocker_pid={blocker_pid}, "
            f"last_observed={last_observed!r}"
        ) from None


async def _seed(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Clinic', 'dental', '+919000000001', 'Asia/Kolkata', 'trial')"
        )
    )
    await seed_whatsapp_channel(session)
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (1, '+919000000001', 'owner', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, is_active) VALUES (1, 1, 'Consultation', 30, 0, 0, true)"
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
            "SELECT 1, day, '09:00', '20:00', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


async def _proposal(session: AsyncSession, key: str) -> tuple[int, int]:
    start, _ = _target()
    service = AppointmentService(session, validation=InternalValidationPort(session))
    proposal = await service.create_proposal(
        CreatePendingAppointmentCommand(
            actor=_customer(),
            service_id=1,
            resource_id=1,
            start_at=start,
            customer_phone=_customer().normalized_phone,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=key,
        )
    )
    await session.commit()
    return proposal.pending_action_id, proposal.version


async def _owner_command(session: AsyncSession, command: str, target_date: str) -> object:
    message = {
        "close_clinic": "close clinic",
        "close_early": "close early at 5 PM",
        "doctor_leave": "Dr. Priya leave",
    }[command]
    return await OwnerCommandService(session, _gateway(command, target_date)).process_command(
        1, "+919000000001", message
    )


@pytest.mark.parametrize("command", ["close_clinic", "close_early", "doctor_leave"])
async def test_schedule_mutation_first_blocks_and_rejects_confirmation(
    pg_session_factory: async_sessionmaker[AsyncSession], command: str
) -> None:
    async with pg_session_factory() as setup:
        await _seed(setup)
        action_id, version = await _proposal(setup, f"mutation-first-{command}")
    _, target_date = _target()

    async with pg_session_factory() as owner_session:
        await _timeouts(owner_session)
        owner_pid = await _pid(owner_session)
        await _owner_command(owner_session, command, target_date)

        blocked_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def confirm() -> Exception | None:
            async with pg_session_factory() as customer_session:
                await _timeouts(customer_session)
                blocked_pid.set_result(await _pid(customer_session))
                service = AppointmentService(
                    customer_session, validation=InternalValidationPort(customer_session)
                )
                try:
                    await service.confirm_and_commit(
                        ConfirmPendingAppointmentCommand(
                            actor=_customer(),
                            pending_action_id=action_id,
                            expected_version=version,
                        )
                    )
                except Exception as exc:
                    await customer_session.rollback()
                    return exc
                await customer_session.commit()
                return None

        task = asyncio.create_task(confirm())
        await _observe_blocker(pg_session_factory, await blocked_pid, owner_pid)
        assert not task.done(), "contender must still be blocked before holder releases"
        await owner_session.commit()
        result = await task
        assert result is not None, "Confirmation must fail after schedule mutation"
        from fonely.api.internal.validation import AppointmentAvailabilityError

        assert isinstance(result, AppointmentAvailabilityError), (
            f"Expected AppointmentAvailabilityError, got {type(result).__name__}: {result}"
        )
        from fonely.services.availability import AvailabilityReason

        expected_reason = {
            "close_clinic": AvailabilityReason.NO_OPERATING_HOURS,
            "close_early": AvailabilityReason.OUTSIDE_OPERATING_HOURS,
            "doctor_leave": AvailabilityReason.NO_OPERATING_HOURS,
        }[command]
        assert result.reason == expected_reason, (
            f"Expected {expected_reason} for {command}, got {result.reason}"
        )

    async with pg_session_factory() as verify:
        assert await verify.scalar(text("SELECT count(*) FROM appointments")) == 0
        assert await verify.scalar(text("SELECT count(*) FROM schedule_exceptions")) == 1
        assert (
            await verify.scalar(
                text("SELECT count(*) FROM resource_allocations WHERE status = 'active'")
            )
            == 0
        )
        assert await verify.scalar(text("SELECT count(*) FROM appointment_commits")) == 0
        assert await verify.scalar(text("SELECT count(*) FROM notification_outbox")) == 0


@pytest.mark.parametrize("command", ["close_clinic", "close_early", "doctor_leave"])
async def test_confirmation_first_is_seen_and_cancelled_by_schedule_mutation(
    pg_session_factory: async_sessionmaker[AsyncSession], command: str
) -> None:
    async with pg_session_factory() as setup:
        await _seed(setup)
        action_id, version = await _proposal(setup, f"confirmation-first-{command}")
    _, target_date = _target()

    async with pg_session_factory() as customer_session:
        await _timeouts(customer_session)
        customer_pid = await _pid(customer_session)
        service = AppointmentService(
            customer_session, validation=InternalValidationPort(customer_session)
        )
        result = await service.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=_customer(), pending_action_id=action_id, expected_version=version
            )
        )
        assert isinstance(result, PreCommitAppointmentSuccess)

        blocked_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def mutate() -> object:
            async with pg_session_factory() as owner_session:
                await _timeouts(owner_session)
                blocked_pid.set_result(await _pid(owner_session))
                owner_result = await _owner_command(owner_session, command, target_date)
                await owner_session.commit()
                return owner_result

        task = asyncio.create_task(mutate())
        await _observe_blocker(pg_session_factory, await blocked_pid, customer_pid)
        assert not task.done(), "contender must still be blocked before holder releases"
        await customer_session.commit()
        owner_result = await task
        assert owner_result.affected_appointments == 1  # type: ignore[attr-defined]

    async with pg_session_factory() as verify:
        assert await verify.scalar(text("SELECT status FROM appointments")) == "cancelled"
        assert (
            await verify.scalar(
                text("SELECT count(*) FROM resource_allocations WHERE status = 'active'")
            )
            == 0
        )
        assert (
            await verify.scalar(
                text("SELECT count(*) FROM appointment_commits WHERE operation = 'cancel'")
            )
            == 1
        )
        assert await verify.scalar(text("SELECT count(*) FROM schedule_exceptions")) == 1
