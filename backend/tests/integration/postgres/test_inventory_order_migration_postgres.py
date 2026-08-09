"""Executable current-schema PostgreSQL contracts for Phase C inventory/orders."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fonely.domain.inventory.commands import SetOwnerStockCommand
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.services.inventory import InventoryService

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 1, 6, tzinfo=UTC)
BACKEND_ROOT = Path(__file__).parents[3]


def owner() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.OWNER,
    )


async def seed(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Migration Shop', 'shop', '+919123456789', "
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
    await session.execute(
        text(
            "INSERT INTO products "
            "(id, business_id, name, unit, price_per_unit, is_active) VALUES "
            "(1, 1, 'Rice', 'kg', 100.00, true)"
        )
    )


@pytest.mark.parametrize(
    "assignment",
    ["on_hand_qty = -1", "reserved_qty = -1", "reserved_qty = 10"],
)
async def test_current_balance_constraints_reject_invalid_values(
    pg_session: AsyncSession, assignment: str
) -> None:
    await seed(pg_session)
    await InventoryService(pg_session).set_stock(
        SetOwnerStockCommand(
            actor=owner(),
            product_id=1,
            quantity="5",
            occurred_at=NOW,
            idempotency_key="constraint-setup",
        )
    )
    with pytest.raises(IntegrityError):
        await pg_session.execute(
            text(f"UPDATE inventory_balances SET {assignment} WHERE product_id = 1")
        )
        await pg_session.flush()


async def test_balance_version_zero_rejected(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await InventoryService(pg_session).set_stock(
        SetOwnerStockCommand(
            actor=owner(),
            product_id=1,
            quantity="5",
            occurred_at=NOW,
            idempotency_key="version-setup",
        )
    )
    with pytest.raises(IntegrityError):
        await pg_session.execute(
            text("UPDATE inventory_balances SET version = 0 WHERE product_id = 1")
        )
        await pg_session.flush()


async def test_movement_update_rejected(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await InventoryService(pg_session).set_stock(
        SetOwnerStockCommand(
            actor=owner(),
            product_id=1,
            quantity="5",
            occurred_at=NOW,
            idempotency_key="append-setup",
        )
    )
    with pytest.raises(IntegrityError, match="append-only"):
        await pg_session.execute(
            text("UPDATE inventory_movements SET note = 'changed' WHERE business_id = 1")
        )
        await pg_session.flush()


async def test_movement_delete_rejected(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    await InventoryService(pg_session).set_stock(
        SetOwnerStockCommand(
            actor=owner(),
            product_id=1,
            quantity="5",
            occurred_at=NOW,
            idempotency_key="delete-setup",
        )
    )
    with pytest.raises(IntegrityError, match="append-only"):
        await pg_session.execute(text("DELETE FROM inventory_movements WHERE business_id = 1"))
        await pg_session.flush()


async def test_movement_coherence_enforced(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    with pytest.raises(IntegrityError):
        await pg_session.execute(
            text(
                "INSERT INTO inventory_movements "
                "(business_id, product_id, business_date, movement_type, "
                "on_hand_delta, reserved_delta, on_hand_after, reserved_after, "
                "available_after) VALUES "
                "(1, 1, '2026-08-01', 'stock_added', 5, 0, 5, 0, 99)"
            )
        )
        await pg_session.flush()


async def test_direct_inventory_idempotent_replay(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    service = InventoryService(pg_session)
    first = await service.set_stock(
        SetOwnerStockCommand(
            actor=owner(),
            product_id=1,
            quantity="10",
            occurred_at=NOW,
            idempotency_key="idem-1",
        )
    )
    assert first.idempotent_replay is False
    second = await service.set_stock(
        SetOwnerStockCommand(
            actor=owner(),
            product_id=1,
            quantity="10",
            occurred_at=NOW,
            idempotency_key="idem-1",
        )
    )
    assert second.idempotent_replay is True
    assert second.movement_id == first.movement_id


async def test_direct_inventory_idempotency_conflict(pg_session: AsyncSession) -> None:
    from fonely.domain.inventory.errors import InventoryIdempotencyConflictError

    await seed(pg_session)
    service = InventoryService(pg_session)
    await service.set_stock(
        SetOwnerStockCommand(
            actor=owner(),
            product_id=1,
            quantity="10",
            occurred_at=NOW,
            idempotency_key="conflict-1",
        )
    )
    with pytest.raises(InventoryIdempotencyConflictError):
        await service.set_stock(
            SetOwnerStockCommand(
                actor=owner(),
                product_id=1,
                quantity="20",
                occurred_at=NOW,
                idempotency_key="conflict-1",
            )
        )


# =============================================================================
# Order line item immutability contracts
# =============================================================================


def _assert_immutability_violation(exc: IntegrityError) -> None:
    """Assert the exact PostgreSQL diagnostic for the order-line immutability trigger."""
    driver = exc.orig
    assert driver is not None
    assert getattr(driver, "sqlstate", None) == "23514"
    name = getattr(driver, "constraint_name", None)
    if name is None:
        cause = driver.__cause__
        assert cause is not None
        name = getattr(cause, "constraint_name", None)
    assert name == "ck_order_line_item_immutable"


async def _seed_order_with_lines(session: AsyncSession) -> int:
    """Seed a complete order with one line item. Returns the order ID."""
    await seed(session)
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) VALUES "
            "(1, '+919222222222', 'owner', true)"
        )
    )
    service = InventoryService(session)
    await service.set_stock(
        SetOwnerStockCommand(
            actor=owner(), product_id=1, quantity="20", occurred_at=NOW, idempotency_key="seed-imm"
        )
    )
    await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "status, expires_at, idempotency_key, version, payload_digest) VALUES "
            "(1, 1, 'order', 1, :payload, 'confirmed', '2026-08-02T00:00:00+05:30', "
            "'pa-imm', 3, 'abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234')"
        ),
        {
            "payload": '{"schema_version":1,"action_type":"order","data":'
            '{"customer_name":"X","customer_phone":"+919222222222",'
            '"pickup_at":"2026-08-01T12:00:00+05:30",'
            '"lines":[{"product_id":1,"quantity":"2"}]}}'
        },
    )
    await session.execute(
        text(
            "INSERT INTO orders (id, business_id, customer_name, customer_phone, "
            "total_amount, status, idempotency_key, pending_action_id) VALUES "
            "(1, 1, 'Customer X', '+919222222222', 200.00, 'confirmed', 'ord-imm', 1)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO order_line_items "
            "(id, business_id, order_id, product_id, product_name_snapshot, qty, unit, "
            "price_per_unit_snapshot, subtotal) VALUES "
            "(1, 1, 1, 1, 'Rice', 2.00, 'kg', 100.00, 200.00)"
        )
    )
    return 1


_LINE_SNAPSHOT_QUERY = (
    "SELECT id, business_id, order_id, product_id, product_name_snapshot, "
    "qty, unit, price_per_unit_snapshot, subtotal FROM order_line_items WHERE id = 1"
)


@pytest.mark.parametrize(
    "mutation",
    [
        "id = 999",
        "qty = 999",
        "product_name_snapshot = 'Altered'",
        "unit = 'piece'",
        "price_per_unit_snapshot = 999.99",
        "subtotal = 999.99",
        "order_id = 999",
        "product_id = 999",
        "business_id = 999",
    ],
)
async def test_order_line_update_rejected_with_exact_constraint(
    pg_session: AsyncSession, mutation: str
) -> None:
    await _seed_order_with_lines(pg_session)
    with pytest.raises(IntegrityError) as exc_info:
        await pg_session.execute(text(f"UPDATE order_line_items SET {mutation} WHERE id = 1"))
        await pg_session.flush()
    _assert_immutability_violation(exc_info.value)


async def test_order_line_delete_rejected_with_exact_constraint(
    pg_session: AsyncSession,
) -> None:
    await _seed_order_with_lines(pg_session)
    with pytest.raises(IntegrityError) as exc_info:
        await pg_session.execute(text("DELETE FROM order_line_items WHERE id = 1"))
        await pg_session.flush()
    _assert_immutability_violation(exc_info.value)


async def test_order_line_unchanged_after_rejected_update(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_order_with_lines(session)
        await session.commit()

    async with pg_session_factory() as session:
        before = (await session.execute(text(_LINE_SNAPSHOT_QUERY))).one()

    async with pg_session_factory() as session:
        try:
            await session.execute(text("UPDATE order_line_items SET qty = 999 WHERE id = 1"))
            await session.commit()
            pytest.fail("UPDATE should have been rejected")
        except IntegrityError:
            await session.rollback()

    async with pg_session_factory() as session:
        after = (await session.execute(text(_LINE_SNAPSHOT_QUERY))).one()
        count = await session.scalar(text("SELECT count(*) FROM order_line_items WHERE id = 1"))
    assert tuple(before) == tuple(after)
    assert count == 1


async def test_order_line_unchanged_after_rejected_delete(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_order_with_lines(session)
        await session.commit()

    async with pg_session_factory() as session:
        before = (await session.execute(text(_LINE_SNAPSHOT_QUERY))).one()

    async with pg_session_factory() as session:
        try:
            await session.execute(text("DELETE FROM order_line_items WHERE id = 1"))
            await session.commit()
            pytest.fail("DELETE should have been rejected")
        except IntegrityError:
            await session.rollback()

    async with pg_session_factory() as session:
        after = (await session.execute(text(_LINE_SNAPSHOT_QUERY))).one()
        count = await session.scalar(text("SELECT count(*) FROM order_line_items WHERE id = 1"))
    assert tuple(before) == tuple(after)
    assert count == 1


async def test_product_mutation_does_not_alter_line_snapshot(pg_session: AsyncSession) -> None:
    await _seed_order_with_lines(pg_session)
    await pg_session.execute(
        text("UPDATE products SET price_per_unit = 999.99, name = 'New Rice' WHERE id = 1")
    )
    line = (
        await pg_session.execute(
            text(
                "SELECT product_name_snapshot, price_per_unit_snapshot "
                "FROM order_line_items WHERE id = 1"
            )
        )
    ).one()
    assert line[0] == "Rice"
    assert line[1] == 100


# =============================================================================
# Populated migration cycle contracts
# =============================================================================


def _alembic_env(database_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return env


async def _clean_and_restore(pg_engine: AsyncEngine, database_url: str) -> None:
    """Remove all test data at the current revision and upgrade to head."""
    async with pg_engine.begin() as conn:
        rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        if rev == "0004":
            tables_0004 = (
                "inventory_movements, inventory_reservations, order_line_items, "
                "orders, inventory_balances, pending_actions, products, "
                "business_users, businesses"
            )
            await conn.execute(text(f"TRUNCATE TABLE {tables_0004} RESTART IDENTITY CASCADE"))
        elif rev in (
            "0005",
            "0006",
            "0007",
            "0008",
            "0009",
            "0010",
            "0011",
            "0012",
            "0013",
            "0014",
            "0015",
        ):
            tables_0005 = (
                "notification_outbox, "
                "business_configuration_commits, business_onboarding_drafts, "
                "inventory_operations, inventory_movements, inventory_reservations, "
                "order_line_items, orders, inventory_balances, pending_actions, "
                "products, business_users, businesses"
            )
            await conn.execute(text(f"TRUNCATE TABLE {tables_0005} RESTART IDENTITY CASCADE"))
    _run_alembic(database_url, "upgrade", "head")


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = _alembic_env(database_url)
    result = subprocess.run(
        [str(BACKEND_ROOT / ".venv" / "bin" / "alembic"), *args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = (result.stderr or "").strip()
        if not msg:
            msg = (result.stdout or "").strip()
        raise RuntimeError(f"alembic {' '.join(args)} failed (rc={result.returncode}): {msg}")
    return result


_PA_JSON_A = (
    '{"schema_version":1,"action_type":"order","data":{"customer_name":"A",'
    '"customer_phone":"+919111111111","pickup_at":"2026-08-01T12:00:00+05:30",'
    '"lines":[{"product_id":1,"quantity":"2"}]}}'
)
_PA_JSON_B = (
    '{"schema_version":1,"action_type":"order","data":{"customer_name":"B",'
    '"customer_phone":"+919222222222","pickup_at":"2026-08-01T14:00:00+05:30",'
    '"lines":[{"product_id":3,"quantity":"1"}]}}'
)


async def _seed_two_tenant_data(conn: AsyncEngine) -> None:  # type: ignore[arg-type]
    """Seed representative two-tenant inventory/order data at revision 0004."""
    await conn.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) VALUES "
            "(1, 'Tenant A Shop', 'shop', '+919100000001', 'Asia/Kolkata', 'trial'), "
            "(2, 'Tenant B Shop', 'shop', '+919200000002', 'Asia/Kolkata', 'trial')"
        )
    )
    await conn.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) VALUES "
            "(1, '+919100000001', 'owner', true), (2, '+919200000002', 'owner', true)"
        )
    )
    await conn.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO products (id, business_id, name, unit, price_per_unit, is_active) VALUES "
            "(1, 1, 'Rice', 'kg', 100.00, true), (2, 1, 'Oil', 'litre', 200.00, true), "
            "(3, 2, 'Flour', 'kg', 50.00, true)"
        )
    )
    await conn.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "confirmation_snapshot, status, expires_at, idempotency_key, version, "
            "payload_digest, committed_entity_type, committed_entity_id) VALUES "
            "(1, 1, 'order', 1, :pa1, "
            "'confirmed', 'confirmed', '2026-08-02T00:00:00+05:30', 'pa-a-1', 3, "
            "'aaaaaaaabbbbbbbbccccccccdddddddd11111111222222223333333344444444', 'order', 1), "
            "(2, 2, 'order', 1, :pa2, "
            "'confirmed', 'confirmed', '2026-08-02T00:00:00+05:30', 'pa-b-1', 3, "
            "'eeeeeeeeffffffffaaaaaaaabbbbbbbb55555555666666667777777788888888', 'order', 2)"
        ),
        {"pa1": _PA_JSON_A, "pa2": _PA_JSON_B},
    )
    await conn.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO inventory_balances "
            "(id, business_id, product_id, business_date, on_hand_qty, reserved_qty, "
            "available_tomorrow, version) VALUES "
            "(1, 1, 1, '2026-08-01', 10.00, 2.00, true, 3), "
            "(2, 1, 2, '2026-08-01', 5.00, 0.00, true, 1), "
            "(3, 2, 3, '2026-08-01', 20.00, 1.00, true, 2)"
        )
    )
    await conn.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO orders "
            "(id, business_id, customer_name, customer_phone, total_amount, status, "
            "idempotency_key, pending_action_id) VALUES "
            "(1, 1, 'Customer A', '+919111111111', 200.00, 'confirmed', 'ord-a-1', 1), "
            "(2, 2, 'Customer B', '+919222222222', 50.00, 'confirmed', 'ord-b-1', 2)"
        )
    )
    await conn.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO order_line_items "
            "(id, order_id, product_id, product_name_snapshot, qty, unit, "
            "price_per_unit_snapshot, subtotal) VALUES "
            "(1, 1, 1, 'Rice', 2.00, 'kg', 100.00, 200.00), "
            "(2, 2, 3, 'Flour', 1.00, 'kg', 50.00, 50.00)"
        )
    )
    await conn.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO inventory_reservations "
            "(id, business_id, product_id, pending_action_id, order_id, business_date, "
            "qty, status, expires_at, idempotency_key) VALUES "
            "(1, 1, 1, 1, 1, '2026-08-01', 2.00, 'active', '2026-08-01T14:00:00+05:30', "
            "'ord-a-1'), "
            "(2, 2, 3, 2, 2, '2026-08-01', 1.00, 'active', '2026-08-01T16:00:00+05:30', "
            "'ord-b-1')"
        )
    )
    await conn.execute(  # type: ignore[union-attr]
        text(
            "INSERT INTO inventory_movements "
            "(id, business_id, product_id, business_date, movement_type, "
            "on_hand_delta, reserved_delta, on_hand_after, reserved_after, "
            "available_after, order_id, reservation_id, pending_action_id) VALUES "
            "(1, 1, 1, '2026-08-01', 'manual_adjustment', 10, 0, 10, 0, 10, NULL, NULL, NULL), "
            "(2, 1, 1, '2026-08-01', 'phone_order_reserved', 0, 2, 10, 2, 8, 1, 1, 1), "
            "(3, 1, 2, '2026-08-01', 'manual_adjustment', 5, 0, 5, 0, 5, NULL, NULL, NULL), "
            "(4, 2, 3, '2026-08-01', 'manual_adjustment', 20, 0, 20, 0, 20, NULL, NULL, NULL), "
            "(5, 2, 3, '2026-08-01', 'phone_order_reserved', 0, 1, 20, 1, 19, 2, 2, 2)"
        )
    )


async def test_populated_migration_cycle(
    pg_engine: AsyncEngine, postgres_database_url: str
) -> None:
    """Prove 0004→0005→0004→0005 with populated two-tenant data."""
    url = postgres_database_url

    _run_alembic(url, "downgrade", "0004")

    async with pg_engine.begin() as conn:
        rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert rev == "0004"
        await _seed_two_tenant_data(conn)

    _run_alembic(url, "upgrade", "head")

    expected_lines = (
        (1, 1, 1, 1, "Rice", 2, "kg", 100, 200),
        (2, 2, 2, 3, "Flour", 1, "kg", 50, 50),
    )
    line_query = (
        "SELECT id, order_id, business_id, product_id, product_name_snapshot, "
        "qty, unit, price_per_unit_snapshot, subtotal "
        "FROM order_line_items ORDER BY id"
    )

    async with pg_engine.begin() as conn:
        rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert rev == "0015"

        lines_after_upgrade = tuple((await conn.execute(text(line_query))).all())
        assert len(lines_after_upgrade) == 2
        for actual, expected in zip(lines_after_upgrade, expected_lines, strict=True):
            assert actual[0] == expected[0]
            assert actual[1] == expected[1]
            assert actual[2] == expected[2]
            assert actual[3] == expected[3]
            assert actual[4] == expected[4]
            assert actual[5] == expected[5]
            assert actual[6] == expected[6]
            assert actual[7] == expected[7]
            assert actual[8] == expected[8]

        bal_t1 = (
            await conn.execute(
                text(
                    "SELECT on_hand_qty, reserved_qty, version "
                    "FROM inventory_balances WHERE business_id=1 AND product_id=1"
                )
            )
        ).one()
        assert tuple(bal_t1) == (10, 2, 3)

        uq_exists = await conn.scalar(
            text("SELECT 1 FROM pg_constraint WHERE conname='uq_products_business_id_id'")
        )
        assert uq_exists == 1

        trigger = await conn.scalar(
            text("SELECT 1 FROM pg_trigger WHERE tgname='ck_inventory_movement_append_only'")
        )
        assert trigger == 1

        line_trigger = await conn.scalar(
            text("SELECT 1 FROM pg_trigger WHERE tgname='ck_order_line_item_immutable'")
        )
        assert line_trigger == 1

        inv_ops = await conn.scalar(text("SELECT count(*) FROM inventory_operations"))
        assert inv_ops == 0

    _run_alembic(url, "downgrade", "0004")

    async with pg_engine.begin() as conn:
        rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert rev == "0004"

        orders_exist = await conn.scalar(text("SELECT count(*) FROM orders"))
        assert orders_exist == 2

        lines_after_downgrade = tuple(
            (
                await conn.execute(
                    text(
                        "SELECT id, order_id, product_id, product_name_snapshot, "
                        "qty, unit, price_per_unit_snapshot, subtotal "
                        "FROM order_line_items ORDER BY id"
                    )
                )
            ).all()
        )
        assert len(lines_after_downgrade) == 2
        for actual, expected in zip(lines_after_downgrade, expected_lines, strict=True):
            assert actual[0] == expected[0]
            assert actual[1] == expected[1]
            assert actual[2] == expected[3]
            assert actual[3] == expected[4]
            assert actual[4] == expected[5]
            assert actual[5] == expected[6]
            assert actual[6] == expected[7]
            assert actual[7] == expected[8]

        has_business_id = await conn.scalar(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='order_line_items' AND column_name='business_id'"
            )
        )
        assert has_business_id is None

    _run_alembic(url, "upgrade", "head")

    async with pg_engine.begin() as conn:
        rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert rev == "0015"

        lines_after_reupgrade = tuple((await conn.execute(text(line_query))).all())
        assert len(lines_after_reupgrade) == 2
        for actual, expected in zip(lines_after_reupgrade, expected_lines, strict=True):
            assert actual[0] == expected[0]
            assert actual[1] == expected[1]
            assert actual[2] == expected[2]
            assert actual[3] == expected[3]
            assert actual[4] == expected[4]
            assert actual[5] == expected[5]
            assert actual[6] == expected[6]
            assert actual[7] == expected[7]
            assert actual[8] == expected[8]

        movement_count = await conn.scalar(text("SELECT count(*) FROM inventory_movements"))
        assert movement_count == 5


async def _assert_migration_rejected(
    pg_engine: AsyncEngine,
    database_url: str,
    direction: str,
    target: str,
    expected_match: str,
) -> None:
    """Assert migration fails with expected message, always restoring head afterward."""
    assertion_error: BaseException | None = None
    try:
        with pytest.raises(RuntimeError, match=expected_match):
            _run_alembic(database_url, direction, target)
    except BaseException as exc:
        assertion_error = exc
    finally:
        try:
            await _clean_and_restore(pg_engine, database_url)
        except Exception as cleanup_exc:
            if assertion_error is not None:
                raise cleanup_exc from assertion_error
            raise
    if assertion_error is not None:
        raise assertion_error


async def test_preflight_rejects_cross_tenant_product_reference(
    pg_engine: AsyncEngine, postgres_database_url: str
) -> None:
    url = postgres_database_url
    _run_alembic(url, "downgrade", "0004")

    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO businesses (id, name, category, primary_contact_phone, "
                "timezone, subscription) VALUES "
                "(1, 'A', 'shop', '+919100000001', 'Asia/Kolkata', 'trial'), "
                "(2, 'B', 'shop', '+919200000002', 'Asia/Kolkata', 'trial')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO products (id, business_id, name, unit, price_per_unit, is_active) "
                "VALUES (1, 1, 'Rice', 'kg', 100.00, true)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO inventory_balances "
                "(business_id, product_id, business_date, on_hand_qty, reserved_qty, "
                "available_tomorrow, version) VALUES "
                "(2, 1, '2026-08-01', 5, 0, true, 1)"
            )
        )

    await _assert_migration_rejected(
        pg_engine, url, "upgrade", "head", "cross-tenant product reference"
    )


async def test_preflight_rejects_duplicate_line_item_product(
    pg_engine: AsyncEngine, postgres_database_url: str
) -> None:
    url = postgres_database_url
    _run_alembic(url, "downgrade", "0004")

    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO businesses (id, name, category, primary_contact_phone, "
                "timezone, subscription) VALUES "
                "(1, 'A', 'shop', '+919100000001', 'Asia/Kolkata', 'trial')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO products (id, business_id, name, unit, price_per_unit, is_active) "
                "VALUES (1, 1, 'Rice', 'kg', 100.00, true)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO orders (id, business_id, customer_phone, total_amount, "
                "status, idempotency_key) VALUES "
                "(1, 1, '+919111111111', 300, 'confirmed', 'ord-1')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO order_line_items "
                "(order_id, product_id, product_name_snapshot, qty, unit, "
                "price_per_unit_snapshot, subtotal) VALUES "
                "(1, 1, 'Rice', 1, 'kg', 100, 100), "
                "(1, 1, 'Rice', 2, 'kg', 100, 200)"
            )
        )

    await _assert_migration_rejected(
        pg_engine, url, "upgrade", "head", "duplicate order line items"
    )


async def test_preflight_rejects_invalid_balance_version(
    pg_engine: AsyncEngine, postgres_database_url: str
) -> None:
    url = postgres_database_url
    _run_alembic(url, "downgrade", "0004")

    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO businesses (id, name, category, primary_contact_phone, "
                "timezone, subscription) VALUES "
                "(1, 'A', 'shop', '+919100000001', 'Asia/Kolkata', 'trial')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO products (id, business_id, name, unit, price_per_unit, is_active) "
                "VALUES (1, 1, 'Rice', 'kg', 100.00, true)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO inventory_balances "
                "(business_id, product_id, business_date, on_hand_qty, reserved_qty, "
                "available_tomorrow, version) VALUES "
                "(1, 1, '2026-08-01', 5, 0, true, 0)"
            )
        )

    await _assert_migration_rejected(pg_engine, url, "upgrade", "head", "version")


async def test_downgrade_preflight_rejects_inventory_operations(
    pg_engine: AsyncEngine, postgres_database_url: str
) -> None:
    url = postgres_database_url
    _run_alembic(url, "downgrade", "0004")
    _run_alembic(url, "upgrade", "head")

    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO businesses (id, name, category, primary_contact_phone, "
                "timezone, subscription) VALUES "
                "(1, 'A', 'shop', '+919100000001', 'Asia/Kolkata', 'trial')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO products (id, business_id, name, unit, price_per_unit, is_active) "
                "VALUES (1, 1, 'Rice', 'kg', 100.00, true)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO inventory_movements "
                "(id, business_id, product_id, business_date, movement_type, "
                "on_hand_delta, reserved_delta, on_hand_after, reserved_after, "
                "available_after) VALUES "
                "(1, 1, 1, '2026-08-01', 'manual_adjustment', 5, 0, 5, 0, 5)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO inventory_operations "
                "(business_id, idempotency_key, operation, product_id, "
                "request_digest, movement_id) VALUES "
                "(1, 'op-1', 'set', 1, "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 1)"
            )
        )

    await _assert_migration_rejected(
        pg_engine, url, "downgrade", "0004", "inventory operation records"
    )
