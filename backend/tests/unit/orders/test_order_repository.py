"""Order repository tenant and transaction-boundary tests."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

from fonely.repositories.orders import OrderRepository


async def test_order_lookup_and_lock_are_tenant_scoped() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.one_or_none.return_value = None
    session.scalars.return_value = result
    repository = OrderRepository(session)
    await repository.lock_by_id(2, 9)
    compiled = str(session.scalars.call_args.args[0])
    assert "orders.business_id" in compiled
    assert "orders.id" in compiled
    assert "FOR UPDATE" in compiled


async def test_due_order_selection_is_deterministic_and_limited_after_ordering() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.scalars.return_value = result
    repository = OrderRepository(session)

    await repository.find_due_order_ids(datetime(2026, 8, 1, tzinfo=UTC), 25)
    compiled = str(session.scalars.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "min(inventory_reservations.expires_at)" in compiled
    assert "GROUP BY inventory_reservations.order_id" in compiled
    assert "ORDER BY anon_1.expires_at, orders.id" in compiled
    assert compiled.index("ORDER BY") < compiled.index("LIMIT")
    assert "FOR UPDATE" in compiled
    assert "SKIP LOCKED" in compiled


def test_repository_has_no_commit_or_rollback_api() -> None:
    assert "commit" not in OrderRepository.__dict__
    assert "rollback" not in OrderRepository.__dict__
