"""Live PostgreSQL contracts for deterministic inventory mutations."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.inventory.commands import (
    AddOwnerStockCommand,
    RecordWalkInSaleCommand,
    SetOwnerStockCommand,
    VerifyLedgerConsistencyQuery,
)
from fonely.domain.inventory.errors import (
    InsufficientAvailableStockError,
    ReservedStockViolationError,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole, Channel
from fonely.services.inventory import InventoryService

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 1, 6, tzinfo=UTC)


def owner() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.OWNER,
        channel=Channel.TEXT,
    )


async def seed(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Inventory Shop', 'shop', '+919123456789', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users "
            "(business_id, phone, role, is_active) "
            "VALUES (1, '+919123456789', 'owner', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO products "
            "(id, business_id, name, unit, price_per_unit, is_active) "
            "VALUES (1, 1, 'Rice', 'kg', 100.00, true)"
        )
    )


async def test_stock_set_add_walk_in_and_ledger(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    service = InventoryService(pg_session)
    set_result = await service.set_stock(
        SetOwnerStockCommand(
            actor=owner(), product_id=1, quantity="10", occurred_at=NOW, idempotency_key="set"
        )
    )
    add_result = await service.add_stock(
        AddOwnerStockCommand(
            actor=owner(), product_id=1, quantity="2", occurred_at=NOW, idempotency_key="add"
        )
    )
    sale = await service.record_walk_in(
        RecordWalkInSaleCommand(
            actor=owner(), product_id=1, quantity="3", occurred_at=NOW, idempotency_key="sale"
        )
    )
    assert set_result.on_hand_after == Decimal("10")
    assert add_result.on_hand_after == Decimal("12")
    assert sale.on_hand_after == Decimal("9")
    assert (await service.verify_ledger(VerifyLedgerConsistencyQuery(business_id=1))).consistent


async def test_stock_set_zero_and_reserved_protection(pg_session: AsyncSession) -> None:
    await seed(pg_session)
    service = InventoryService(pg_session)
    await service.set_stock(
        SetOwnerStockCommand(
            actor=owner(), product_id=1, quantity="0", occurred_at=NOW, idempotency_key="zero"
        )
    )
    await pg_session.execute(
        text(
            "UPDATE inventory_balances SET on_hand_qty=5, reserved_qty=3 "
            "WHERE business_id=1 AND product_id=1"
        )
    )
    with pytest.raises(ReservedStockViolationError):
        await service.set_stock(
            SetOwnerStockCommand(
                actor=owner(), product_id=1, quantity="2", occurred_at=NOW, idempotency_key="bad"
            )
        )
    with pytest.raises(InsufficientAvailableStockError):
        await service.record_walk_in(
            RecordWalkInSaleCommand(
                actor=owner(),
                product_id=1,
                quantity="3",
                occurred_at=NOW,
                idempotency_key="bad-sale",
            )
        )
