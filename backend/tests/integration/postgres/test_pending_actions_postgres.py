"""PostgreSQL integration contracts for Phase B.

Collected everywhere; skipped only when FONELY_TEST_DATABASE_URL is absent.
Each concurrent task uses a separate AsyncSession.
"""

import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fonely.core.validators import utcnow
from fonely.domain.pending_actions.commands import (
    ActorContext,
    BeginCommitCommand,
    BulkExpirePendingActionsCommand,
    CommitResultContext,
    CompleteCommitCommand,
    CreatePendingActionCommand,
    GetActivePendingActionQuery,
    GetPendingActionQuery,
    MarkAwaitingConfirmationCommand,
)
from fonely.domain.pending_actions.errors import (
    InvalidStateTransitionError,
    PendingActionConcurrencyError,
    PendingActionExpiredError,
    PendingActionIdempotencyConflictError,
    PendingActionNotFoundError,
    PendingActionUnauthorizedError,
    TrustedCommitContextError,
)
from fonely.models.enums import CallerRole, Channel, PendingActionType
from fonely.models.schema import PendingAction
from fonely.services.authorization import require_owner_or_manager
from fonely.services.pending_actions import PendingActionService

pytestmark = pytest.mark.postgres
BACKEND_ROOT = Path(__file__).parents[3]


def _run_alembic(database_url: str, *args: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [str(BACKEND_ROOT / ".venv" / "bin" / "alembic"), *args],
        cwd=BACKEND_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def actor(business_id: int) -> ActorContext:
    return ActorContext(
        business_id=business_id,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
        session_id=f"session-{business_id}",
    )


def payload(
    quantity: str = "2.00",
    pickup_at: datetime | None = None,
    product_id: int = 1,
) -> dict[str, object]:
    pickup_at = pickup_at or utcnow() + timedelta(hours=2)
    return {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_name": "Example Customer",
            "customer_phone": "+919123456789",
            "pickup_at": pickup_at.isoformat(),
            "lines": [{"product_id": product_id, "quantity": quantity}],
            "customer_note": None,
        },
    }


def command(
    business_id: int,
    key: str = "key-1",
    quantity: str = "2.00",
    *,
    expires_at: datetime | None = None,
    pickup_at: datetime | None = None,
    product_id: int | None = None,
) -> CreatePendingActionCommand:
    return CreatePendingActionCommand(
        actor=actor(business_id),
        action_type=PendingActionType.ORDER,
        payload_schema_version=1,
        payload=payload(
            quantity,
            pickup_at=pickup_at,
            product_id=product_id or business_id,
        ),
        expires_at=expires_at or utcnow() + timedelta(minutes=15),
        idempotency_key=key,
    )


async def _seed_business(session: AsyncSession, business_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:id, :name, 'shop', :phone, 'Asia/Kolkata', 'trial') "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": business_id,
            "name": f"Test Business {business_id}",
            "phone": f"+9190000000{business_id:02d}",
        },
    )
    await session.execute(
        text(
            "INSERT INTO products "
            "(id, business_id, name, unit, price_per_unit, is_active) "
            "VALUES (:id, :business_id, :name, 'kg', 100.00, true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": business_id,
            "business_id": business_id,
            "name": f"Test Product {business_id}",
        },
    )


async def _create_and_await_confirmation(
    session: AsyncSession,
    business_id: int,
    key: str,
) -> tuple[int, int]:
    service = PendingActionService(session)
    created = await service.create(command(business_id, key=key))
    awaiting = await service.mark_awaiting_confirmation(
        MarkAwaitingConfirmationCommand(
            actor=actor(business_id),
            action_id=created.id,
            expected_version=created.version,
        )
    )
    return awaiting.id, awaiting.version


async def test_0002_migrates_populated_0001_database(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
) -> None:
    _run_alembic(postgres_database_url, "downgrade", "0001")
    legacy_payload = {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_name": "Legacy Customer",
            "customer_phone": "+919123456789",
            "pickup_at": (utcnow() + timedelta(hours=2)).isoformat(),
            "lines": [{"product_id": 1, "quantity": "1.00"}],
            "customer_note": None,
        },
    }
    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO businesses "
                "(id, name, category, primary_contact_phone, timezone, subscription) "
                "VALUES (1, 'Legacy Business', 'shop', '+919000000001', "
                "'Asia/Kolkata', 'trial')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO products "
                "(id, business_id, name, unit, price_per_unit, is_active) "
                "VALUES (1, 1, 'Legacy Product', 'kg', 100.00, true)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO pending_actions "
                "(id, business_id, action_type, payload_schema_version, proposed_payload, "
                "status, expires_at, idempotency_key, version) "
                "VALUES (1, 1, 'order', 1, CAST(:payload AS jsonb), "
                "'collecting_details', :expires, 'legacy', 1)"
            ),
            {
                "payload": json.dumps(legacy_payload),
                "expires": utcnow() + timedelta(minutes=15),
            },
        )
    _run_alembic(postgres_database_url, "upgrade", "head")
    async with pg_engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT payload_digest, rejection_reason_code FROM pending_actions WHERE id=1")
            )
        ).one()
        nullable = await connection.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name='pending_actions' AND column_name='payload_digest'"
            )
        )
    assert isinstance(row.payload_digest, str)
    assert len(row.payload_digest) == 64
    assert row.rejection_reason_code is None
    assert nullable == "NO"


async def test_migrations_through_head_are_applied(pg_session: AsyncSession) -> None:
    revision = await pg_session.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "0015"


@pytest.mark.parametrize(
    ("column", "valid", "low", "high"),
    [
        ("appointment_booking_horizon_days", 90, 0, 366),
        ("appointment_minimum_notice_minutes", 0, -1, 10081),
        ("appointment_slot_interval_minutes", 15, 4, 121),
    ],
)
async def test_appointment_policy_bounds_are_enforced(
    pg_session: AsyncSession,
    column: str,
    valid: int,
    low: int,
    high: int,
) -> None:
    await _seed_business(pg_session, 1)
    await pg_session.execute(
        text(f"UPDATE businesses SET {column} = :value WHERE id = 1"), {"value": valid}
    )
    for invalid in (low, high):
        with pytest.raises(IntegrityError):
            await pg_session.execute(
                text(f"UPDATE businesses SET {column} = :value WHERE id = 1"),
                {"value": invalid},
            )
        await pg_session.rollback()
        await _seed_business(pg_session, 1)


async def _seed_appointment_catalog(session: AsyncSession) -> None:
    await _seed_business(session, 1)
    await session.execute(
        text(
            "INSERT INTO services (id, business_id, name, duration_minutes, price, is_active) "
            "VALUES (1, 1, 'Haircut', 30, 500.00, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Priya', 'staff', true)"
        )
    )


async def _insert_confirmed_appointment(
    session: AsyncSession,
    appointment_id: int = 1,
    *,
    status: str = "confirmed",
) -> None:
    start = datetime(2026, 8, 3, 10, tzinfo=utcnow().tzinfo)
    end = start + timedelta(minutes=30)
    await session.execute(
        text(
            "INSERT INTO appointments "
            "(id, business_id, resource_id, service_id, customer_phone, start_at, end_at, "
            "effective_start_at, effective_end_at, service_name_snapshot, "
            "resource_name_snapshot, duration_minutes_snapshot, "
            "buffer_before_minutes_snapshot, buffer_after_minutes_snapshot, "
            "business_timezone_snapshot, status, cancelled_at, source, "
            "idempotency_key, version, created_at, updated_at) "
            "VALUES (:id, 1, 1, 1, '+919123456789', :start, :end, :start, :end, "
            "'Haircut', 'Priya', 30, 0, 0, 'Asia/Kolkata', CAST(:status AS text), "
            "CASE WHEN CAST(:status AS text) = 'cancelled' THEN now() ELSE NULL END, "
            "'owner_manual', :key, 1, now(), now())"
        ),
        {
            "id": appointment_id,
            "start": start,
            "end": end,
            "status": status,
            "key": f"appt-{appointment_id}",
        },
    )


async def test_confirmed_appointment_requires_allocation_at_deferred_boundary(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_appointment_catalog(session)
        await _insert_confirmed_appointment(session)
        with pytest.raises(IntegrityError) as error:
            await session.commit()
        assert getattr(error.value.orig, "sqlstate", None) == "23514"
        cause = error.value.orig
        while cause is not None:
            cn = getattr(cause, "constraint_name", None)
            if cn is not None:
                break
            cause = getattr(cause, "__cause__", None)
        assert cn == "ck_confirmed_appointment_active_allocation"


async def test_same_transaction_confirmed_appointment_and_matching_allocation_succeeds(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_appointment_catalog(session)
        await _insert_confirmed_appointment(session)
        appointment = (
            await session.execute(
                text("SELECT effective_start_at, effective_end_at FROM appointments WHERE id = 1")
            )
        ).one()
        await session.execute(
            text(
                "INSERT INTO resource_allocations "
                "(business_id, resource_id, appointment_id, allocation_type, status, source, "
                "effective_start_at, effective_end_at, idempotency_key, version) "
                "VALUES (1, 1, 1, 'manual_appointment', 'active', 'owner_manual', "
                ":start, :end, 'allocation-1', 1)"
            ),
            {"start": appointment.effective_start_at, "end": appointment.effective_end_at},
        )
        await session.commit()


async def test_cancelled_appointment_does_not_require_active_allocation(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_appointment_catalog(session)
        await _insert_confirmed_appointment(session, status="cancelled")
        await session.commit()


@pytest.mark.parametrize("status", ["completed", "no_show"])
async def test_terminal_appointment_without_allocation_is_rejected(
    pg_session_factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    past = datetime(2020, 1, 1, 10, tzinfo=utcnow().tzinfo)
    past_end = past + timedelta(minutes=30)
    async with pg_session_factory() as session:
        await _seed_appointment_catalog(session)
        await session.execute(
            text(
                "INSERT INTO appointments "
                "(id, business_id, resource_id, service_id, customer_phone, start_at, end_at, "
                "effective_start_at, effective_end_at, service_name_snapshot, "
                "resource_name_snapshot, duration_minutes_snapshot, "
                "buffer_before_minutes_snapshot, buffer_after_minutes_snapshot, "
                "business_timezone_snapshot, status, source, "
                "idempotency_key, version, created_at, updated_at) "
                "VALUES (1, 1, 1, 1, '+919123456789', :start, :end, :start, :end, "
                "'Haircut', 'Priya', 30, 0, 0, 'Asia/Kolkata', CAST(:status AS text), "
                "'owner_manual', 'appt-terminal-1', 1, now(), now())"
            ),
            {"start": past, "end": past_end, "status": status},
        )
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        cause = error.value.orig
        while cause is not None:
            cn = getattr(cause, "constraint_name", None)
            if cn is not None:
                break
            cause = getattr(cause, "__cause__", None)
        assert cn == "ck_confirmed_appointment_active_allocation"
        await session.rollback()


async def test_same_business_equivalent_create_returns_one_action(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    service = PendingActionService(pg_session)
    request = command(1)
    first = await service.create(request)
    second = await service.create(request)
    assert first.id == second.id
    count = await pg_session.scalar(select(func.count(PendingAction.id)))
    assert count == 1


async def test_same_key_different_business_is_allowed(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    await _seed_business(pg_session, 2)
    service = PendingActionService(pg_session)
    first = await service.create(command(1, key="shared"))
    second = await service.create(command(2, key="shared"))
    assert first.id != second.id


async def test_same_business_key_different_payload_conflicts(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    service = PendingActionService(pg_session)
    expires_at = utcnow() + timedelta(minutes=15)
    pickup_at = utcnow() + timedelta(hours=2)
    await service.create(command(1, quantity="2.00", expires_at=expires_at, pickup_at=pickup_at))
    with pytest.raises(PendingActionIdempotencyConflictError):
        await service.create(
            command(1, quantity="3.00", expires_at=expires_at, pickup_at=pickup_at)
        )


async def test_concurrent_create_produces_one_row(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as seed:
        await _seed_business(seed, 1)
        await seed.commit()

    request = command(1, key="concurrent-create")

    async def create_once() -> int:
        async with pg_session_factory() as session:
            result = await PendingActionService(session).create(request)
            await session.commit()
            return result.id

    first, second = await asyncio.gather(create_once(), create_once())
    assert first == second


async def test_concurrent_begin_commit_exactly_one_succeeds(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as seed:
        await _seed_business(seed, 1)
        action_id, version = await _create_and_await_confirmation(seed, 1, "concurrent-begin")
        await seed.commit()

    async def begin_once() -> str:
        async with pg_session_factory() as session:
            try:
                await PendingActionService(session).begin_commit(
                    BeginCommitCommand(
                        context=CommitResultContext(
                            business_id=1,
                            pending_action_id=action_id,
                            expected_version=version,
                            engine="order_engine",
                        )
                    )
                )
                await session.commit()
                return "success"
            except PendingActionConcurrencyError:
                await session.rollback()
                return "stale"
            except InvalidStateTransitionError:
                await session.rollback()
                return "wrong_state"

    outcomes = await asyncio.gather(begin_once(), begin_once())
    assert outcomes.count("success") == 1
    assert outcomes.count("stale") + outcomes.count("wrong_state") == 1


async def test_stale_expected_version_rejected(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    action_id, _ = await _create_and_await_confirmation(pg_session, 1, "stale")
    with pytest.raises(PendingActionConcurrencyError):
        await PendingActionService(pg_session).begin_commit(
            BeginCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=action_id,
                    expected_version=1,
                    engine="order_engine",
                )
            )
        )


async def test_wrong_state_rejected(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    created = await PendingActionService(pg_session).create(command(1, key="wrong-state"))
    with pytest.raises(InvalidStateTransitionError):
        await PendingActionService(pg_session).begin_commit(
            BeginCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=created.id,
                    expected_version=1,
                    engine="order_engine",
                )
            )
        )


async def test_exact_expiry_boundary_rejected(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    action_id, version = await _create_and_await_confirmation(pg_session, 1, "expiry")
    boundary = utcnow()
    await pg_session.execute(
        text("UPDATE pending_actions SET expires_at=:now WHERE id=:id"),
        {"now": boundary, "id": action_id},
    )
    with (
        patch("fonely.services.pending_actions.utcnow", return_value=boundary),
        pytest.raises(PendingActionExpiredError),
    ):
        await PendingActionService(pg_session).begin_commit(
            BeginCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=action_id,
                    expected_version=version,
                    engine="order_engine",
                )
            )
        )


async def test_bulk_expiry_is_idempotent(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    now = utcnow()
    await pg_session.execute(
        text(
            "INSERT INTO pending_actions "
            "(business_id, action_type, payload_schema_version, proposed_payload, "
            "payload_digest, status, expires_at, idempotency_key, version) "
            "VALUES (1, 'order', 1, '{}'::jsonb, :digest, 'collecting_details', "
            ":expires, 'expired-key', 1)"
        ),
        {"digest": "0" * 64, "expires": now},
    )
    service = PendingActionService(pg_session)
    first = await service.bulk_expire(BulkExpirePendingActionsCommand(now=now))
    second = await service.bulk_expire(BulkExpirePendingActionsCommand(now=now))
    assert first.count == 1
    assert second.count == 0


async def test_cross_tenant_lookup_returns_not_found(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    await _seed_business(pg_session, 2)
    created = await PendingActionService(pg_session).create(command(1, key="tenant"))
    result = await PendingActionService(pg_session).get_active(
        GetActivePendingActionQuery(
            actor=actor(2),
            session_id="session-1",
            action_type=PendingActionType.ORDER,
        )
    )
    assert result is None
    with pytest.raises(PendingActionNotFoundError):
        await PendingActionService(pg_session).get(
            GetPendingActionQuery(actor=actor(2), action_id=created.id)
        )


async def test_invalid_enum_string_rejected_by_postgres(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    with pytest.raises(IntegrityError):
        await pg_session.execute(
            text(
                "INSERT INTO pending_actions "
                "(business_id, action_type, payload_schema_version, proposed_payload, "
                "payload_digest, status, expires_at, idempotency_key, version) "
                "VALUES (1, 'order', 1, '{}'::jsonb, :digest, 'invalid_status', "
                ":expires, 'invalid-enum', 1)"
            ),
            {"digest": "0" * 64, "expires": utcnow() + timedelta(minutes=15)},
        )


async def test_transaction_rollback_leaves_no_partial_action(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    service = PendingActionService(pg_session)
    created = await service.create(command(1, key="rollback"))
    assert created.id > 0
    await pg_session.rollback()
    count = await pg_session.scalar(
        select(func.count(PendingAction.id)).where(PendingAction.idempotency_key == "rollback")
    )
    assert count == 0


async def test_session_fixture_keeps_database_at_current_head(
    pg_session: AsyncSession,
) -> None:
    """Session fixture keeps the database at the current head during tests."""
    revision = await pg_session.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "0015"


async def test_retry_after_idempotency_is_tenant_scoped(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    result = await PendingActionService(pg_session).create(command(1, key="retry"))
    assert result.business_id == 1


async def test_active_owner_membership_is_tenant_scoped(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    await _seed_business(pg_session, 2)
    await pg_session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (1, :phone, 'owner', true)"
        ),
        {"phone": "+919123456789"},
    )
    owner_actor = ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.OWNER,
        channel=Channel.TEXT,
    )
    user = await require_owner_or_manager(pg_session, owner_actor)
    assert user.business_id == 1

    cross_tenant_actor = owner_actor.model_copy(update={"business_id": 2})
    with pytest.raises(PendingActionUnauthorizedError):
        await require_owner_or_manager(pg_session, cross_tenant_actor)


async def test_inactive_owner_membership_is_rejected(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, 1)
    await pg_session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (1, :phone, 'owner', false)"
        ),
        {"phone": "+919123456789"},
    )
    owner_actor = ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.OWNER,
        channel=Channel.TEXT,
    )
    with pytest.raises(PendingActionUnauthorizedError):
        await require_owner_or_manager(pg_session, owner_actor)


async def test_complete_commit_requires_exact_pending_action_link(
    pg_session: AsyncSession,
) -> None:
    await _seed_business(pg_session, 1)
    action_id, version = await _create_and_await_confirmation(pg_session, 1, "linked-order")
    service = PendingActionService(pg_session)
    committing = await service.begin_commit(
        BeginCommitCommand(
            context=CommitResultContext(
                business_id=1,
                pending_action_id=action_id,
                expected_version=version,
                engine="order_engine",
            )
        )
    )
    order_id = await pg_session.scalar(
        text(
            "INSERT INTO orders "
            "(business_id, customer_phone, total_amount, status, idempotency_key, "
            "pending_action_id) VALUES "
            "(1, '+919123456789', 100.00, 'confirmed', 'linked-order-row', :pending) "
            "RETURNING id"
        ),
        {"pending": action_id},
    )
    assert order_id is not None
    completion = CompleteCommitCommand(
        context=CommitResultContext(
            business_id=1,
            pending_action_id=action_id,
            expected_version=committing.version,
            engine="order_engine",
        ),
        committed_entity_type="order",
        committed_entity_id=order_id,
    )
    confirmed = await service.complete_commit(completion)
    assert confirmed.committed_entity_id == order_id
    assert (await service.complete_commit(completion)).committed_entity_id == order_id


async def test_same_business_wrong_pending_action_entity_rejected(
    pg_session: AsyncSession,
) -> None:
    await _seed_business(pg_session, 1)
    first_id, _ = await _create_and_await_confirmation(pg_session, 1, "first-action")
    second_id, second_version = await _create_and_await_confirmation(pg_session, 1, "second-action")
    service = PendingActionService(pg_session)
    second_committing = await service.begin_commit(
        BeginCommitCommand(
            context=CommitResultContext(
                business_id=1,
                pending_action_id=second_id,
                expected_version=second_version,
                engine="order_engine",
            )
        )
    )
    order_id = await pg_session.scalar(
        text(
            "INSERT INTO orders "
            "(business_id, customer_phone, total_amount, status, idempotency_key, "
            "pending_action_id) VALUES "
            "(1, '+919123456789', 100.00, 'confirmed', 'wrong-link-row', :pending) "
            "RETURNING id"
        ),
        {"pending": first_id},
    )
    assert order_id is not None
    with pytest.raises(TrustedCommitContextError):
        await service.complete_commit(
            CompleteCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=second_id,
                    expected_version=second_committing.version,
                    engine="order_engine",
                ),
                committed_entity_type="order",
                committed_entity_id=order_id,
            )
        )


async def test_cross_business_committed_entity_is_rejected(
    pg_session: AsyncSession,
) -> None:
    await _seed_business(pg_session, 1)
    await _seed_business(pg_session, 2)
    action_id, version = await _create_and_await_confirmation(pg_session, 1, "cross-business-link")
    service = PendingActionService(pg_session)
    committing = await service.begin_commit(
        BeginCommitCommand(
            context=CommitResultContext(
                business_id=1,
                pending_action_id=action_id,
                expected_version=version,
                engine="order_engine",
            )
        )
    )
    order_id = await pg_session.scalar(
        text(
            "INSERT INTO orders "
            "(business_id, customer_phone, total_amount, status, idempotency_key) "
            "VALUES (2, '+919123456789', 100.00, 'confirmed', 'cross-business-row') "
            "RETURNING id"
        )
    )
    assert order_id is not None
    with pytest.raises(TrustedCommitContextError):
        await service.complete_commit(
            CompleteCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=action_id,
                    expected_version=committing.version,
                    engine="order_engine",
                ),
                committed_entity_type="order",
                committed_entity_id=order_id,
            )
        )


async def test_nonexistent_committed_entity_is_rejected(
    pg_session: AsyncSession,
) -> None:
    await _seed_business(pg_session, 1)
    action_id, version = await _create_and_await_confirmation(pg_session, 1, "nonexistent-link")
    service = PendingActionService(pg_session)
    committing = await service.begin_commit(
        BeginCommitCommand(
            context=CommitResultContext(
                business_id=1,
                pending_action_id=action_id,
                expected_version=version,
                engine="order_engine",
            )
        )
    )
    with pytest.raises(TrustedCommitContextError):
        await service.complete_commit(
            CompleteCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=action_id,
                    expected_version=committing.version,
                    engine="order_engine",
                ),
                committed_entity_type="order",
                committed_entity_id=999999,
            )
        )


async def test_two_orders_cannot_reference_same_pending_action(
    pg_session: AsyncSession,
) -> None:
    await _seed_business(pg_session, 1)
    created = await PendingActionService(pg_session).create(command(1, key="unique-link"))
    base_sql = text(
        "INSERT INTO orders "
        "(business_id, customer_phone, total_amount, status, idempotency_key, "
        "pending_action_id) VALUES "
        "(1, '+919123456789', 100.00, 'confirmed', :key, :pending)"
    )
    await pg_session.execute(base_sql, {"key": "order-one", "pending": created.id})
    with pytest.raises(IntegrityError):
        async with pg_session.begin_nested():
            await pg_session.execute(
                base_sql,
                {"key": "order-two", "pending": created.id},
            )
