"""Inventory repository SQL policy tests."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fonely.repositories.inventory import InventoryRepository


async def test_product_and_balance_locks_are_tenant_scoped_and_ordered() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.scalars.return_value = result
    repository = InventoryRepository(session)
    await repository.lock_active_products(7, [3, 1, 3])
    product_sql = str(session.scalars.call_args.args[0])
    assert "products.business_id" in product_sql
    assert "products.id IN" in product_sql
    assert "ORDER BY products.id" in product_sql
    assert "FOR UPDATE" in product_sql

    await repository.lock_balances(7, [3, 1, 3], datetime(2026, 8, 1, tzinfo=UTC).date())
    balance_sql = str(session.scalars.call_args.args[0])
    assert "inventory_balances.business_id" in balance_sql
    assert "inventory_balances.product_id IN" in balance_sql
    assert "ORDER BY inventory_balances.product_id" in balance_sql
    assert "FOR UPDATE" in balance_sql


async def test_due_reservations_are_bounded_and_skip_locked() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.scalars.return_value = result
    repository = InventoryRepository(session)

    await repository.lock_due_reservations(datetime(2026, 8, 1, tzinfo=UTC), 25)
    compiled = str(session.scalars.call_args.args[0])
    assert "inventory_reservations.expires_at <=" in compiled
    assert "ORDER BY inventory_reservations.expires_at" in compiled
    assert "LIMIT" in compiled
    assert "FOR UPDATE" in compiled
