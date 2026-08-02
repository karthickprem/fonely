"""Executable current-schema PostgreSQL contracts for Phase C inventory/orders.

Future migration 0005 requirements are specifications in the final Phase C handoff,
not executable tests in this module.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.inventory.commands import SetOwnerStockCommand
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.services.inventory import InventoryService

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 1, 6, tzinfo=UTC)


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
