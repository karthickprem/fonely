"""Live PostgreSQL contracts for deterministic order confirmation and lifecycle."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.inventory.commands import (
    SetOwnerStockCommand,
)
from fonely.domain.orders.commands import (
    CancelOrderCommand,
    CompletePickupCommand,
    ConfirmOrderLine,
    ConfirmPendingOrderCommand,
    ExpireOrderReservationsCommand,
    GetOrderQuery,
)
from fonely.domain.orders.errors import (
    OrderStateTransitionError,
)
from fonely.domain.orders.results import OrderResult
from fonely.domain.pending_actions.commands import (
    ActorContext,
    CommitResultContext,
    CreatePendingActionCommand,
    GetPendingActionQuery,
    InternalGetPendingActionQuery,
    MarkAwaitingConfirmationCommand,
)
from fonely.models.enums import CallerRole, OrderStatus, PendingActionType
from fonely.services.inventory import InventoryService
from fonely.services.orders import OrderService
from fonely.services.pending_actions import PendingActionService

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 1, 6, tzinfo=UTC)
EXPIRES = NOW + timedelta(hours=2)


def owner() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.OWNER,
    )


def customer() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919222222222",
        verified_role=CallerRole.CUSTOMER,
        session_id="session-1",
    )


async def seed(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Order Shop', 'shop', '+919123456789', 'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users "
            "(business_id, phone, role, is_active) VALUES "
            "(1, '+919123456789', 'owner', true)"
        )
    )
    roles = tuple(
        (await session.scalars(text("SELECT role FROM business_users ORDER BY phone"))).all()
    )
    assert roles == ("owner",)
    await session.execute(
        text(
            "INSERT INTO products "
            "(id, business_id, name, unit, price_per_unit, is_active) VALUES "
            "(1, 1, 'Rice', 'kg', 100.00, true), "
            "(2, 1, 'Oil', 'litre', 200.00, true)"
        )
    )


async def seed_stock(session: AsyncSession) -> None:
    inv = InventoryService(session)
    await inv.set_stock(
        SetOwnerStockCommand(
            actor=owner(),
            product_id=1,
            quantity="20",
            occurred_at=NOW,
            idempotency_key="seed-stock-1",
        )
    )
    await inv.set_stock(
        SetOwnerStockCommand(
            actor=owner(),
            product_id=2,
            quantity="15",
            occurred_at=NOW,
            idempotency_key="seed-stock-2",
        )
    )


async def create_pending_order(
    session: AsyncSession,
    lines: list[dict[str, object]] | None = None,
    key: str = "pending-1",
) -> int:
    pending_service = PendingActionService(session)
    default_lines = [{"product_id": 1, "quantity": "2"}]
    result = await pending_service.create(
        CreatePendingActionCommand(
            actor=customer(),
            action_type=PendingActionType.ORDER,
            payload={
                "schema_version": 1,
                "action_type": "order",
                "data": {
                    "customer_name": "Test Customer",
                    "customer_phone": "+919222222222",
                    "pickup_at": (NOW + timedelta(hours=1)).isoformat(),
                    "lines": lines or default_lines,
                },
            },
            expires_at=datetime.now(UTC) + timedelta(hours=12),
            idempotency_key=key,
        )
    )
    await pending_service.mark_awaiting_confirmation(
        MarkAwaitingConfirmationCommand(
            actor=customer(),
            action_id=result.id,
            expected_version=result.version,
        )
    )
    updated = await pending_service.get(
        GetPendingActionQuery(actor=customer(), action_id=result.id)
    )
    return updated.id


async def confirm_order(
    session: AsyncSession,
    pending_action_id: int,
    lines: tuple[ConfirmOrderLine, ...] | None = None,
    key: str = "order-1",
    version: int = 3,
) -> OrderResult:
    order_service = OrderService(session)
    pending_service = PendingActionService(session)
    pending = await pending_service.internal_get(
        InternalGetPendingActionQuery(
            business_id=1,
            action_id=pending_action_id,
        )
    )
    default_lines = (ConfirmOrderLine(product_id=1, quantity="2"),)
    return await order_service.confirm(
        ConfirmPendingOrderCommand(
            context=CommitResultContext(
                business_id=1,
                pending_action_id=pending_action_id,
                expected_version=pending.version,
                engine="order_engine",
            ),
            actor=customer(),
            lines=lines or default_lines,
            now=NOW,
            reservation_expires_at=EXPIRES,
            idempotency_key=key,
        )
    )


# --- Single-item confirmation ---


async def test_single_item_confirmation(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(pg_session)
    result = await confirm_order(pg_session, pa_id)
    assert result.status == OrderStatus.CONFIRMED
    assert len(result.lines) == 1
    assert result.lines[0].product_name == "Rice"
    assert result.lines[0].price_per_unit == Decimal("100.00")
    assert result.lines[0].subtotal == Decimal("200.00")
    assert result.total_amount == Decimal("200.00")
    persisted = (
        await pg_session.execute(
            text(
                "SELECT o.status, o.total_amount, p.status, p.committed_entity_type, "
                "p.committed_entity_id, r.status, r.qty, b.reserved_qty, "
                "m.reserved_delta FROM orders o "
                "JOIN pending_actions p ON p.id=o.pending_action_id "
                "JOIN inventory_reservations r ON r.order_id=o.id "
                "JOIN inventory_balances b ON b.business_id=o.business_id "
                " AND b.product_id=r.product_id AND b.business_date=r.business_date "
                "JOIN inventory_movements m ON m.reservation_id=r.id "
                "WHERE o.id=:order_id"
            ),
            {"order_id": result.id},
        )
    ).one()
    assert tuple(persisted) == (
        "confirmed",
        Decimal("200.00"),
        "confirmed",
        "order",
        result.id,
        "active",
        Decimal("2.00"),
        Decimal("2.00"),
        Decimal("2.00"),
    )


# --- Multi-item confirmation ---


async def test_multi_item_confirmation(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(
        pg_session,
        lines=[
            {"product_id": 1, "quantity": "2"},
            {"product_id": 2, "quantity": "1"},
        ],
        key="pending-multi",
    )
    result = await confirm_order(
        pg_session,
        pa_id,
        lines=(
            ConfirmOrderLine(product_id=1, quantity="2"),
            ConfirmOrderLine(product_id=2, quantity="1"),
        ),
        key="order-multi",
    )
    assert result.status == OrderStatus.CONFIRMED
    assert len(result.lines) == 2
    assert result.total_amount == Decimal("400.00")


# --- Authoritative price snapshots ---


async def test_price_snapshots_are_authoritative(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(pg_session)
    result = await confirm_order(pg_session, pa_id)
    assert result.lines[0].price_per_unit == Decimal("100.00")

    await pg_session.execute(text("UPDATE products SET price_per_unit = 999.00 WHERE id = 1"))
    order_service = OrderService(pg_session)
    read_result = await order_service.get(GetOrderQuery(actor=customer(), order_id=result.id))
    assert read_result.lines[0].price_per_unit == Decimal("100.00")


# --- Cross-tenant read rejection ---


async def test_cross_tenant_order_read_rejected(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(pg_session)
    result = await confirm_order(pg_session, pa_id)

    await pg_session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (2, 'Other Shop', 'shop', '+919999999999', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    other_customer = ActorContext(
        business_id=2,
        normalized_phone="+919999999999",
        verified_role=CallerRole.CUSTOMER,
    )
    order_service = OrderService(pg_session)
    from fonely.domain.orders.errors import OrderNotFoundError

    with pytest.raises(OrderNotFoundError):
        await order_service.get(GetOrderQuery(actor=other_customer, order_id=result.id))
    assert await pg_session.scalar(text("SELECT count(*) FROM orders WHERE business_id=2")) == 0


# --- Cancellation releases all ---


async def test_cancellation_releases_all(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(pg_session)
    confirmed = await confirm_order(pg_session, pa_id)

    order_service = OrderService(pg_session)
    cancelled = await order_service.cancel(
        CancelOrderCommand(
            actor=customer(),
            order_id=confirmed.id,
            now=NOW,
            idempotency_key="cancel-1",
        )
    )
    assert cancelled.status == OrderStatus.CANCELLED
    persisted = (
        await pg_session.execute(
            text(
                "SELECT o.status, r.status, b.reserved_qty, m.reserved_delta "
                "FROM orders o JOIN inventory_reservations r ON r.order_id=o.id "
                "JOIN inventory_balances b ON b.product_id=r.product_id "
                " AND b.business_id=r.business_id AND b.business_date=r.business_date "
                "JOIN inventory_movements m ON m.reservation_id=r.id "
                " AND m.movement_type='order_cancelled' WHERE o.id=:order_id"
            ),
            {"order_id": confirmed.id},
        )
    ).one()
    assert tuple(persisted) == (
        "cancelled",
        "released",
        Decimal("0.00"),
        Decimal("-2.00"),
    )


async def test_duplicate_cancellation_no_second_delta(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(pg_session)
    confirmed = await confirm_order(pg_session, pa_id)

    order_service = OrderService(pg_session)
    await order_service.cancel(
        CancelOrderCommand(
            actor=customer(),
            order_id=confirmed.id,
            now=NOW,
            idempotency_key="cancel-dup",
        )
    )
    movement_count = await pg_session.scalar(
        text(
            "SELECT count(*) FROM inventory_movements "
            "WHERE order_id=:order_id AND movement_type='order_cancelled'"
        ),
        {"order_id": confirmed.id},
    )
    replay = await order_service.cancel(
        CancelOrderCommand(
            actor=customer(),
            order_id=confirmed.id,
            now=NOW,
            idempotency_key="cancel-dup-2",
        )
    )
    assert replay.idempotent_replay is True
    assert (
        await pg_session.scalar(
            text(
                "SELECT count(*) FROM inventory_movements "
                "WHERE order_id=:order_id AND movement_type='order_cancelled'"
            ),
            {"order_id": confirmed.id},
        )
        == movement_count
        == 1
    )


# --- Expiry ---


async def test_expiry_releases_complete_order_once(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(pg_session)
    confirmed = await confirm_order(pg_session, pa_id)

    service = OrderService(pg_session)
    first = await service.expire(ExpireOrderReservationsCommand(now=EXPIRES, batch_size=10))
    second = await service.expire(ExpireOrderReservationsCommand(now=EXPIRES, batch_size=10))
    assert first.expired_order_ids == (confirmed.id,)
    assert first.count == 1
    assert second.count == 0
    persisted = (
        await pg_session.execute(
            text(
                "SELECT o.status, r.status, b.reserved_qty, m.reserved_delta, "
                "(SELECT count(*) FROM inventory_movements m2 "
                " WHERE m2.reservation_id=r.id "
                " AND m2.movement_type='reservation_released') "
                "FROM orders o JOIN inventory_reservations r ON r.order_id=o.id "
                "JOIN inventory_balances b ON b.product_id=r.product_id "
                " AND b.business_id=r.business_id AND b.business_date=r.business_date "
                "JOIN inventory_movements m ON m.reservation_id=r.id "
                " AND m.movement_type='reservation_released' WHERE o.id=:order_id"
            ),
            {"order_id": confirmed.id},
        )
    ).one()
    assert tuple(persisted) == (
        "cancelled",
        "expired",
        Decimal("0.00"),
        Decimal("-2.00"),
        1,
    )


# --- Pickup ---


async def test_pickup_decrements_both(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(pg_session)
    confirmed = await confirm_order(pg_session, pa_id)

    order_service = OrderService(pg_session)
    picked = await order_service.complete_pickup(
        CompletePickupCommand(
            actor=owner(),
            order_id=confirmed.id,
            now=NOW + timedelta(minutes=30),
            idempotency_key="pickup-1",
        )
    )
    assert picked.status == OrderStatus.PICKED_UP
    persisted = (
        await pg_session.execute(
            text(
                "SELECT o.status, r.status, b.on_hand_qty, b.reserved_qty, "
                "m.on_hand_delta, m.reserved_delta FROM orders o "
                "JOIN inventory_reservations r ON r.order_id=o.id "
                "JOIN inventory_balances b ON b.product_id=r.product_id "
                " AND b.business_id=r.business_id AND b.business_date=r.business_date "
                "JOIN inventory_movements m ON m.reservation_id=r.id "
                " AND m.movement_type='order_completed' WHERE o.id=:order_id"
            ),
            {"order_id": confirmed.id},
        )
    ).one()
    assert tuple(persisted) == (
        "picked_up",
        "committed",
        Decimal("18.00"),
        Decimal("0.00"),
        Decimal("-2.00"),
        Decimal("-2.00"),
    )


async def test_cancelled_order_cannot_pickup(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(pg_session)
    confirmed = await confirm_order(pg_session, pa_id)

    order_service = OrderService(pg_session)
    await order_service.cancel(
        CancelOrderCommand(
            actor=customer(),
            order_id=confirmed.id,
            now=NOW,
            idempotency_key="cancel-before-pickup",
        )
    )
    before = (
        await pg_session.execute(
            text(
                "SELECT o.status, r.status, b.on_hand_qty, b.reserved_qty, "
                "(SELECT count(*) FROM inventory_movements m "
                " WHERE m.order_id=o.id) "
                "FROM orders o JOIN inventory_reservations r ON r.order_id=o.id "
                "JOIN inventory_balances b ON b.business_id=o.business_id "
                " AND b.product_id=r.product_id AND b.business_date=r.business_date "
                "WHERE o.business_id=1 AND o.id=:order_id"
            ),
            {"order_id": confirmed.id},
        )
    ).one()
    with pytest.raises(OrderStateTransitionError, match="cancelled order"):
        await order_service.complete_pickup(
            CompletePickupCommand(
                actor=owner(),
                order_id=confirmed.id,
                now=NOW + timedelta(minutes=30),
                idempotency_key="pickup-cancelled",
            )
        )
    after = (
        await pg_session.execute(
            text(
                "SELECT o.status, r.status, b.on_hand_qty, b.reserved_qty, "
                "(SELECT count(*) FROM inventory_movements m "
                " WHERE m.order_id=o.id) "
                "FROM orders o JOIN inventory_reservations r ON r.order_id=o.id "
                "JOIN inventory_balances b ON b.business_id=o.business_id "
                " AND b.product_id=r.product_id AND b.business_date=r.business_date "
                "WHERE o.business_id=1 AND o.id=:order_id"
            ),
            {"order_id": confirmed.id},
        )
    ).one()
    assert (
        tuple(before)
        == tuple(after)
        == (
            "cancelled",
            "released",
            Decimal("20.00"),
            Decimal("0.00"),
            3,
        )
    )
    assert (
        await pg_session.scalar(
            text(
                "SELECT count(*) FROM inventory_movements "
                "WHERE business_id=1 AND order_id=:order_id "
                "AND movement_type='order_completed'"
            ),
            {"order_id": confirmed.id},
        )
        == 0
    )


async def test_picked_up_order_cannot_cancel(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(pg_session)
    confirmed = await confirm_order(pg_session, pa_id)

    order_service = OrderService(pg_session)
    await order_service.complete_pickup(
        CompletePickupCommand(
            actor=owner(),
            order_id=confirmed.id,
            now=NOW + timedelta(minutes=30),
            idempotency_key="pickup-first",
        )
    )
    with pytest.raises(OrderStateTransitionError):
        await order_service.cancel(
            CancelOrderCommand(
                actor=customer(),
                order_id=confirmed.id,
                now=NOW + timedelta(minutes=31),
                idempotency_key="cancel-after-pickup",
            )
        )


# --- Ledger after order lifecycle ---


async def test_ledger_consistent_after_order_lifecycle(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await seed_stock(pg_session)
    pa_id = await create_pending_order(pg_session)
    await confirm_order(pg_session, pa_id)

    inv = InventoryService(pg_session)
    from fonely.domain.inventory.commands import VerifyLedgerConsistencyQuery

    result = await inv.verify_ledger(VerifyLedgerConsistencyQuery(business_id=1))
    assert result.consistent
