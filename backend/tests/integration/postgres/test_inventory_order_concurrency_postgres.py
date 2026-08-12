"""Observed-lock PostgreSQL concurrency contracts for Phase C."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.domain.inventory.commands import SetOwnerStockCommand
from fonely.domain.inventory.errors import (
    InsufficientAvailableStockError,
    InventoryIdempotencyConflictError,
)
from fonely.domain.orders.commands import ConfirmOrderLine, ConfirmPendingOrderCommand
from fonely.domain.orders.results import OrderResult
from fonely.domain.pending_actions.commands import (
    ActorContext,
    CommitResultContext,
    CreatePendingActionCommand,
    MarkAwaitingConfirmationCommand,
)
from fonely.domain.pending_actions.errors import PendingActionConcurrencyError
from fonely.models.enums import CallerRole, Channel, PendingActionType
from fonely.services.inventory import InventoryService
from fonely.services.orders import OrderService
from fonely.services.pending_actions import PendingActionService
from tests.integration.postgres.concurrency import (
    install_transaction_timeouts,
)
from tests.integration.postgres.concurrency import (
    observe_lock_contention as observed_blocker,
)

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 1, 6, tzinfo=UTC)
EXPIRY = NOW + timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    kind: Literal["success", "concurrency", "insufficient"]
    result: OrderResult | Exception


def owner() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.OWNER,
        channel=Channel.TEXT,
    )


def customer(session_id: str) -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919222222222",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
        session_id=session_id,
    )


async def seed(session: AsyncSession, *, quantity: str = "1") -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Concurrency Shop', 'shop', '+919123456789', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users "
            "(business_id, phone, role, is_active) VALUES "
            "(1, '+919123456789', 'owner', true)"
        )
    )
    roles = tuple((await session.scalars(text("SELECT role FROM business_users"))).all())
    assert roles == ("owner",)
    await session.execute(
        text(
            "INSERT INTO products "
            "(id, business_id, name, unit, price_per_unit, is_active) VALUES "
            "(1, 1, 'Rice', 'kg', 100.00, true), "
            "(2, 1, 'Oil', 'litre', 200.00, true)"
        )
    )
    inventory = InventoryService(session)
    for product_id in (1, 2):
        await inventory.set_stock(
            SetOwnerStockCommand(
                actor=owner(),
                product_id=product_id,
                quantity=quantity,
                occurred_at=NOW,
                idempotency_key=f"seed-{product_id}",
            )
        )
    await session.commit()


async def create_action(
    session: AsyncSession,
    *,
    action_key: str,
    session_id: str,
    lines: tuple[tuple[int, str], ...],
) -> tuple[int, int]:
    action_actor = customer(session_id)
    service = PendingActionService(session)
    created = await service.create(
        CreatePendingActionCommand(
            actor=action_actor,
            action_type=PendingActionType.ORDER,
            payload={
                "schema_version": 1,
                "action_type": "order",
                "data": {
                    "customer_name": "Concurrent Customer",
                    "customer_phone": action_actor.normalized_phone,
                    "pickup_at": (NOW + timedelta(hours=1)).isoformat(),
                    "lines": [
                        {"product_id": product_id, "quantity": quantity}
                        for product_id, quantity in lines
                    ],
                },
            },
            expires_at=datetime.now(UTC) + timedelta(hours=12),
            idempotency_key=action_key,
        )
    )
    awaiting = await service.mark_awaiting_confirmation(
        MarkAwaitingConfirmationCommand(
            actor=action_actor,
            action_id=created.id,
            expected_version=created.version,
        )
    )
    await session.commit()
    return awaiting.id, awaiting.version


def confirmation_command(
    *,
    action_id: int,
    version: int,
    session_id: str,
    order_key: str,
    lines: tuple[tuple[int, str], ...],
) -> ConfirmPendingOrderCommand:
    return ConfirmPendingOrderCommand(
        context=CommitResultContext(
            business_id=1,
            pending_action_id=action_id,
            expected_version=version,
            engine="order_engine",
        ),
        actor=customer(session_id),
        lines=tuple(
            ConfirmOrderLine(product_id=product_id, quantity=quantity)
            for product_id, quantity in lines
        ),
        now=NOW,
        reservation_expires_at=EXPIRY,
        idempotency_key=order_key,
    )


async def backend_pid(session: AsyncSession) -> int:
    value = await session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(value, int)
    return value


async def lock_inventory_rows(
    session: AsyncSession, product_ids: tuple[int, ...]
) -> tuple[int, list[int], list[int]]:
    ids = tuple(sorted(set(product_ids)))
    placeholders = ", ".join(str(product_id) for product_id in ids)
    await install_transaction_timeouts(session)
    pid = await backend_pid(session)
    product_locks = list(
        (
            await session.scalars(
                text(
                    "SELECT id FROM products WHERE business_id=1 "
                    f"AND id IN ({placeholders}) ORDER BY id FOR UPDATE"
                )
            )
        ).all()
    )
    balance_locks = list(
        (
            await session.scalars(
                text(
                    "SELECT id FROM inventory_balances WHERE business_id=1 "
                    f"AND product_id IN ({placeholders}) "
                    "ORDER BY product_id FOR UPDATE"
                )
            )
        ).all()
    )
    return pid, product_locks, balance_locks


async def lock_pending_action(session: AsyncSession, action_id: int) -> int:
    await install_transaction_timeouts(session)
    pid = await backend_pid(session)
    locked_id = await session.scalar(
        text("SELECT id FROM pending_actions WHERE id=:id FOR UPDATE"),
        {"id": action_id},
    )
    assert locked_id == action_id
    return pid


async def confirmation_worker(
    factory: async_sessionmaker[AsyncSession],
    *,
    command: ConfirmPendingOrderCommand,
    pid_ready: asyncio.Future[int],
) -> AttemptOutcome:
    async with factory() as session:
        await install_transaction_timeouts(session)
        pid_ready.set_result(await backend_pid(session))
        try:
            result = await OrderService(session).confirm(command)
            await session.commit()
            return AttemptOutcome("success", result)
        except PendingActionConcurrencyError as exc:
            await session.rollback()
            return AttemptOutcome("concurrency", exc)
        except InsufficientAvailableStockError as exc:
            await session.rollback()
            return AttemptOutcome("insufficient", exc)


async def cancel_task(task: asyncio.Task[AttemptOutcome]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@dataclass(slots=True)
class HeldConfirmation:
    pid: int
    task: asyncio.Task[AttemptOutcome]


async def held_confirmation_worker(
    factory: async_sessionmaker[AsyncSession],
    *,
    command: ConfirmPendingOrderCommand,
    pid_ready: asyncio.Future[int],
    confirmed: asyncio.Event,
    release: asyncio.Event,
) -> AttemptOutcome:
    async with factory() as session:
        await install_transaction_timeouts(session)
        pid_ready.set_result(await backend_pid(session))
        try:
            result = await OrderService(session).confirm(command)
            confirmed.set()
            await release.wait()
            await session.commit()
            return AttemptOutcome("success", result)
        except PendingActionConcurrencyError as exc:
            await session.rollback()
            return AttemptOutcome("concurrency", exc)
        except InsufficientAvailableStockError as exc:
            await session.rollback()
            return AttemptOutcome("insufficient", exc)


async def start_held_confirmation(
    factory: async_sessionmaker[AsyncSession],
    command: ConfirmPendingOrderCommand,
    confirmed: asyncio.Event,
    release: asyncio.Event,
) -> HeldConfirmation:
    pid_ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        held_confirmation_worker(
            factory,
            command=command,
            pid_ready=pid_ready,
            confirmed=confirmed,
            release=release,
        )
    )
    pid = await asyncio.wait_for(pid_ready, timeout=2)
    return HeldConfirmation(pid=pid, task=task)


async def test_final_stock_order_race_has_exactly_one_winner(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await install_transaction_timeouts(setup)
        await seed(setup, quantity="1")
        first = await create_action(
            setup,
            action_key="race-action-1",
            session_id="race-1",
            lines=((1, "1"),),
        )
        second = await create_action(
            setup,
            action_key="race-action-2",
            session_id="race-2",
            lines=((1, "1"),),
        )

    async with pg_session_factory() as winner_session:
        winner_pid, products, balances = await lock_inventory_rows(winner_session, (1,))
        assert len(products) == len(balances) == 1
        loop = asyncio.get_running_loop()
        loser_pid_ready: asyncio.Future[int] = loop.create_future()
        loser_task = asyncio.create_task(
            confirmation_worker(
                pg_session_factory,
                command=confirmation_command(
                    action_id=second[0],
                    version=second[1],
                    session_id="race-2",
                    order_key="race-order-2",
                    lines=((1, "1"),),
                ),
                pid_ready=loser_pid_ready,
            )
        )
        try:
            loser_pid = await asyncio.wait_for(loser_pid_ready, timeout=2)
            await observed_blocker(
                pg_session_factory,
                blocked_pid=loser_pid,
                expected_blocker_pid=winner_pid,
            )
            assert not loser_task.done(), "contender must still be blocked before holder releases"
            winner = await OrderService(winner_session).confirm(
                confirmation_command(
                    action_id=first[0],
                    version=first[1],
                    session_id="race-1",
                    order_key="race-order-1",
                    lines=((1, "1"),),
                )
            )
            await winner_session.commit()
            loser = await asyncio.wait_for(loser_task, timeout=5)
        finally:
            await cancel_task(loser_task)

    assert winner.id > 0
    assert loser.kind == "insufficient"
    async with pg_session_factory() as verify:
        await install_transaction_timeouts(verify)
        counts = (
            await verify.execute(
                text(
                    "SELECT (SELECT count(*) FROM orders WHERE business_id=1), "
                    "(SELECT count(*) FROM inventory_reservations WHERE business_id=1), "
                    "(SELECT count(*) FROM inventory_movements "
                    " WHERE business_id=1 AND movement_type='phone_order_reserved')"
                )
            )
        ).one()
        balance = (
            await verify.execute(
                text(
                    "SELECT on_hand_qty, reserved_qty FROM inventory_balances "
                    "WHERE business_id=1 AND product_id=1"
                )
            )
        ).one()
        states = tuple(
            (
                await verify.scalars(
                    text("SELECT status FROM pending_actions WHERE business_id=1 ORDER BY id")
                )
            ).all()
        )
    assert counts == (1, 1, 1)
    assert tuple(balance) == (1, 1)
    assert states == ("confirmed", "awaiting_confirmation")


async def test_duplicate_confirmation_race_has_one_effect(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await install_transaction_timeouts(setup)
        await seed(setup, quantity="2")
        action_id, version = await create_action(
            setup,
            action_key="duplicate-action",
            session_id="duplicate",
            lines=((1, "1"),),
        )

    command = confirmation_command(
        action_id=action_id,
        version=version,
        session_id="duplicate",
        order_key="duplicate-order",
        lines=((1, "1"),),
    )
    async with pg_session_factory() as winner_session:
        winner_pid = await lock_pending_action(winner_session, action_id)
        loop = asyncio.get_running_loop()
        loser_pid_ready: asyncio.Future[int] = loop.create_future()
        loser_task = asyncio.create_task(
            confirmation_worker(
                pg_session_factory,
                command=command,
                pid_ready=loser_pid_ready,
            )
        )
        try:
            loser_pid = await asyncio.wait_for(loser_pid_ready, timeout=2)
            await observed_blocker(
                pg_session_factory,
                blocked_pid=loser_pid,
                expected_blocker_pid=winner_pid,
            )
            assert not loser_task.done(), "contender must still be blocked before holder releases"
            winner = await OrderService(winner_session).confirm(command)
            await winner_session.commit()
            loser = await asyncio.wait_for(loser_task, timeout=5)
        finally:
            await cancel_task(loser_task)

    assert winner.id > 0
    assert loser.kind == "concurrency"
    async with pg_session_factory() as verify:
        await install_transaction_timeouts(verify)
        counts = (
            await verify.execute(
                text(
                    "SELECT (SELECT count(*) FROM orders WHERE business_id=1), "
                    "(SELECT count(*) FROM order_line_items li JOIN orders o ON o.id=li.order_id "
                    " WHERE o.business_id=1), "
                    "(SELECT count(*) FROM inventory_reservations WHERE business_id=1), "
                    "(SELECT count(*) FROM inventory_movements "
                    " WHERE business_id=1 AND movement_type='phone_order_reserved')"
                )
            )
        ).one()
        linkage = (
            await verify.execute(
                text(
                    "SELECT p.status, p.committed_entity_id, o.id "
                    "FROM pending_actions p JOIN orders o "
                    "ON o.pending_action_id=p.id WHERE p.business_id=1 "
                    "AND o.business_id=1 AND p.id=:id"
                ),
                {"id": action_id},
            )
        ).one()
    assert counts == (1, 1, 1, 1)
    assert tuple(linkage) == ("confirmed", winner.id, winner.id)


async def test_reversed_multi_product_race_does_not_deadlock(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await install_transaction_timeouts(setup)
        await seed(setup, quantity="4")
        first = await create_action(
            setup,
            action_key="reverse-action-1",
            session_id="reverse-1",
            lines=((1, "1"), (2, "1")),
        )
        second = await create_action(
            setup,
            action_key="reverse-action-2",
            session_id="reverse-2",
            lines=((2, "1"), (1, "1")),
        )

    first_command = confirmation_command(
        action_id=first[0],
        version=first[1],
        session_id="reverse-1",
        order_key="reverse-order-1",
        lines=((1, "1"), (2, "1")),
    )
    second_command = confirmation_command(
        action_id=second[0],
        version=second[1],
        session_id="reverse-2",
        order_key="reverse-order-2",
        lines=((2, "1"), (1, "1")),
    )
    assert [line.product_id for line in first_command.lines] == [1, 2]
    assert [line.product_id for line in second_command.lines] == [1, 2]

    first_confirmed = asyncio.Event()
    release_first = asyncio.Event()
    first_held = await start_held_confirmation(
        pg_session_factory, first_command, first_confirmed, release_first
    )
    second_pid_ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    second_task: asyncio.Task[AttemptOutcome] | None = None
    try:
        await asyncio.wait_for(first_confirmed.wait(), timeout=5)
        second_task = asyncio.create_task(
            confirmation_worker(
                pg_session_factory,
                command=second_command,
                pid_ready=second_pid_ready,
            )
        )
        second_pid = await asyncio.wait_for(second_pid_ready, timeout=2)
        assert first_held.pid != second_pid
        await observed_blocker(
            pg_session_factory,
            blocked_pid=second_pid,
            expected_blocker_pid=first_held.pid,
        )
        assert not first_held.task.done()
        assert not second_task.done()
        release_first.set()
        first_outcome, second_outcome = await asyncio.wait_for(
            asyncio.gather(first_held.task, second_task), timeout=10
        )
    finally:
        release_first.set()
        await cancel_task(first_held.task)
        if second_task is not None:
            await cancel_task(second_task)

    assert first_outcome.kind == second_outcome.kind == "success"
    async with pg_session_factory() as verify:
        await install_transaction_timeouts(verify)
        counts = (
            await verify.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM orders WHERE business_id=1), "
                    "(SELECT count(*) FROM order_line_items li JOIN orders o ON o.id=li.order_id "
                    " WHERE o.business_id=1), "
                    "(SELECT count(*) FROM inventory_reservations WHERE business_id=1), "
                    "(SELECT count(*) FROM inventory_movements "
                    " WHERE business_id=1 AND movement_type='phone_order_reserved')"
                )
            )
        ).one()
        balances = tuple(
            (
                await verify.execute(
                    text(
                        "SELECT product_id, on_hand_qty, reserved_qty "
                        "FROM inventory_balances WHERE business_id=1 ORDER BY product_id"
                    )
                )
            ).all()
        )
        movement_totals = tuple(
            (
                await verify.execute(
                    text(
                        "SELECT product_id, sum(reserved_delta) "
                        "FROM inventory_movements WHERE business_id=1 "
                        "GROUP BY product_id ORDER BY product_id"
                    )
                )
            ).all()
        )
        linkages = tuple(
            (
                await verify.execute(
                    text(
                        "SELECT p.id, p.status, p.committed_entity_id, o.id "
                        "FROM pending_actions p JOIN orders o "
                        "ON o.pending_action_id=p.id "
                        "WHERE p.business_id=1 AND o.business_id=1 ORDER BY p.id"
                    )
                )
            ).all()
        )
    assert counts == (2, 4, 4, 4)
    assert balances == ((1, 4, 2), (2, 4, 2))
    assert movement_totals == ((1, 2), (2, 2))
    assert len(linkages) == 2
    assert all(
        status == "confirmed" and entity_id == order_id
        for _, status, entity_id, order_id in linkages
    )


async def direct_inventory_worker(
    factory: async_sessionmaker[AsyncSession],
    *,
    quantity: str,
    key: str,
    pid_ready: asyncio.Future[int],
) -> tuple[str, object]:
    async with factory() as session:
        await install_transaction_timeouts(session)
        pid_ready.set_result(await backend_pid(session))
        try:
            result = await InventoryService(session).set_stock(
                SetOwnerStockCommand(
                    actor=owner(),
                    product_id=1,
                    quantity=quantity,
                    occurred_at=NOW,
                    idempotency_key=key,
                )
            )
            await session.commit()
            return ("success", result)
        except InventoryIdempotencyConflictError as exc:
            await session.rollback()
            return ("conflict", exc)
        except Exception as exc:
            await session.rollback()
            return ("error", exc)


async def test_direct_inventory_post_serialization_replay(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same product + same key + same digest: product locks serialize the sessions,
    so the loser finds the winner's operation during the post-lock recheck.
    Both return equivalent success; one operation, one movement."""
    async with pg_session_factory() as setup:
        await install_transaction_timeouts(setup)
        await seed(setup, quantity="10")

    loop = asyncio.get_running_loop()
    first_ready: asyncio.Future[int] = loop.create_future()
    second_ready: asyncio.Future[int] = loop.create_future()
    first_task = asyncio.create_task(
        direct_inventory_worker(
            pg_session_factory, quantity="5", key="race-direct", pid_ready=first_ready
        )
    )
    second_task = asyncio.create_task(
        direct_inventory_worker(
            pg_session_factory, quantity="5", key="race-direct", pid_ready=second_ready
        )
    )
    try:
        results = await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=15)
    finally:
        await cancel_task(first_task)
        await cancel_task(second_task)

    kinds = sorted(r[0] for r in results)
    assert kinds == ["success", "success"]
    async with pg_session_factory() as verify:
        await install_transaction_timeouts(verify)
        op_count = await verify.scalar(
            text("SELECT count(*) FROM inventory_operations WHERE idempotency_key = 'race-direct'")
        )
        race_movement_count = await verify.scalar(
            text(
                "SELECT count(*) FROM inventory_movements m "
                "JOIN inventory_operations o ON o.movement_id = m.id "
                "WHERE o.idempotency_key = 'race-direct'"
            )
        )
    assert op_count == 1
    assert race_movement_count == 1


async def direct_inventory_product_worker(
    factory: async_sessionmaker[AsyncSession],
    *,
    product_id: int,
    quantity: str,
    key: str,
    pid_ready: asyncio.Future[int],
    gate: asyncio.Event,
) -> tuple[str, object]:
    """Worker that waits at gate before calling set_stock, ensuring both sessions
    have opened transactions and done their initial operation lookup before either
    proceeds to lock products."""
    async with factory() as session:
        await install_transaction_timeouts(session)
        pid_ready.set_result(await backend_pid(session))
        await gate.wait()
        try:
            result = await InventoryService(session).set_stock(
                SetOwnerStockCommand(
                    actor=owner(),
                    product_id=product_id,
                    quantity=quantity,
                    occurred_at=NOW,
                    idempotency_key=key,
                )
            )
            await session.commit()
            return ("success", result)
        except InventoryIdempotencyConflictError as exc:
            await session.rollback()
            return ("conflict", exc)
        except Exception as exc:
            await session.rollback()
            return ("error", exc)


async def test_direct_inventory_different_product_unique_race(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Different products + same key → different digests. Product locks do NOT
    serialize the sessions, so both reach operation insert. One wins the unique
    constraint; the loser hits real PostgreSQL 23505 on uq_inv_op_idempotency,
    its savepoint rolls back the movement/balance, and the service returns a
    typed InventoryIdempotencyConflictError after rereading the winner."""
    async with pg_session_factory() as setup:
        await install_transaction_timeouts(setup)
        await seed(setup, quantity="10")

    async with pg_session_factory() as baseline:
        await install_transaction_timeouts(baseline)
        seed_max_movement = await baseline.scalar(
            text("SELECT COALESCE(max(id), 0) FROM inventory_movements WHERE business_id = 1")
        )
        seed_balances = dict(
            (
                await baseline.execute(
                    text(
                        "SELECT product_id, version FROM inventory_balances "
                        "WHERE business_id = 1 ORDER BY product_id"
                    )
                )
            ).all()
        )

    loop = asyncio.get_running_loop()
    gate = asyncio.Event()
    p1_ready: asyncio.Future[int] = loop.create_future()
    p2_ready: asyncio.Future[int] = loop.create_future()
    task1 = asyncio.create_task(
        direct_inventory_product_worker(
            pg_session_factory,
            product_id=1,
            quantity="3",
            key="unique-race-key",
            pid_ready=p1_ready,
            gate=gate,
        )
    )
    task2 = asyncio.create_task(
        direct_inventory_product_worker(
            pg_session_factory,
            product_id=2,
            quantity="3",
            key="unique-race-key",
            pid_ready=p2_ready,
            gate=gate,
        )
    )
    try:
        pid1 = await asyncio.wait_for(p1_ready, timeout=3)
        pid2 = await asyncio.wait_for(p2_ready, timeout=3)
        assert pid1 != pid2
        gate.set()
        results = await asyncio.wait_for(asyncio.gather(task1, task2), timeout=15)
    finally:
        await cancel_task(task1)
        await cancel_task(task2)

    kinds = sorted(r[0] for r in results)
    assert kinds == ["conflict", "success"], f"Expected one success + one conflict, got {kinds}"

    async with pg_session_factory() as verify:
        await install_transaction_timeouts(verify)

        op_count = await verify.scalar(
            text(
                "SELECT count(*) FROM inventory_operations "
                "WHERE idempotency_key = 'unique-race-key'"
            )
        )
        assert op_count == 1

        winner_op = (
            await verify.execute(
                text(
                    "SELECT product_id, movement_id FROM inventory_operations "
                    "WHERE idempotency_key = 'unique-race-key'"
                )
            )
        ).one()
        winner_product = winner_op[0]
        winner_movement_id = winner_op[1]
        loser_product = 2 if winner_product == 1 else 1

        winner_movement = (
            await verify.execute(
                text(
                    "SELECT business_id, product_id, movement_type, "
                    "on_hand_delta, reserved_delta, on_hand_after, reserved_after, "
                    "business_date "
                    "FROM inventory_movements WHERE id = :mid"
                ),
                {"mid": winner_movement_id},
            )
        ).one()
        assert winner_movement[0] == 1
        assert winner_movement[1] == winner_product
        assert winner_movement[2] == "manual_adjustment"
        assert winner_movement[3] == -7
        assert winner_movement[4] == 0
        assert winner_movement[5] == 3
        assert winner_movement[6] == 0
        assert str(winner_movement[7]) == "2026-08-01"
        assert winner_movement_id > seed_max_movement

        winner_race_count = await verify.scalar(
            text(
                "SELECT count(*) FROM inventory_movements "
                "WHERE business_id = 1 AND product_id = :pid AND id > :baseline"
            ),
            {"pid": winner_product, "baseline": seed_max_movement},
        )
        assert winner_race_count == 1

        loser_race_movements = await verify.scalar(
            text(
                "SELECT count(*) FROM inventory_movements "
                "WHERE business_id = 1 AND product_id = :pid AND id > :baseline"
            ),
            {"pid": loser_product, "baseline": seed_max_movement},
        )
        assert loser_race_movements == 0

        loser_balance = (
            await verify.execute(
                text(
                    "SELECT on_hand_qty, reserved_qty, version "
                    "FROM inventory_balances "
                    "WHERE business_id = 1 AND product_id = :pid"
                ),
                {"pid": loser_product},
            )
        ).one()
        assert loser_balance[0] == 10
        assert loser_balance[1] == 0
        assert loser_balance[2] == seed_balances[loser_product]

        winner_balance = (
            await verify.execute(
                text(
                    "SELECT on_hand_qty, reserved_qty, version "
                    "FROM inventory_balances "
                    "WHERE business_id = 1 AND product_id = :pid"
                ),
                {"pid": winner_product},
            )
        ).one()
        assert winner_balance[0] == 3
        assert winner_balance[1] == 0
        assert winner_balance[2] == seed_balances[winner_product] + 1


async def test_direct_inventory_same_key_changed_semantics_conflict(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        await install_transaction_timeouts(setup)
        await seed(setup, quantity="10")

    async with pg_session_factory() as first_session:
        await install_transaction_timeouts(first_session)
        await InventoryService(first_session).set_stock(
            SetOwnerStockCommand(
                actor=owner(),
                product_id=1,
                quantity="5",
                occurred_at=NOW,
                idempotency_key="conflict-key",
            )
        )
        await first_session.commit()

    async with pg_session_factory() as second_session:
        await install_transaction_timeouts(second_session)
        with pytest.raises(InventoryIdempotencyConflictError):
            await InventoryService(second_session).set_stock(
                SetOwnerStockCommand(
                    actor=owner(),
                    product_id=1,
                    quantity="7",
                    occurred_at=NOW,
                    idempotency_key="conflict-key",
                )
            )
