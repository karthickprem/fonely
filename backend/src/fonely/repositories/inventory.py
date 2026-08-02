"""Tenant-scoped inventory persistence within a caller-owned transaction."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.enums import InventoryReservationStatus
from fonely.models.schema import (
    Business,
    InventoryBalance,
    InventoryMovement,
    InventoryOperation,
    InventoryReservation,
    Product,
)


class InventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_business_timezone(self, business_id: int) -> str | None:
        value = await self._session.scalar(
            select(Business.timezone).where(Business.id == business_id)
        )
        return str(value) if value is not None else None

    async def get_active_products(
        self, business_id: int, product_ids: Sequence[int]
    ) -> tuple[Product, ...]:
        ids = sorted(set(product_ids))
        statement = select(Product).where(
            Product.business_id == business_id,
            Product.is_active.is_(True),
        )
        if ids:
            statement = statement.where(Product.id.in_(ids))
        statement = statement.order_by(Product.id)
        return tuple((await self._session.scalars(statement)).all())

    async def lock_active_products(
        self, business_id: int, product_ids: Sequence[int]
    ) -> tuple[Product, ...]:
        ids = sorted(set(product_ids))
        statement = (
            select(Product)
            .where(
                Product.business_id == business_id,
                Product.id.in_(ids),
                Product.is_active.is_(True),
            )
            .order_by(Product.id)
            .with_for_update()
        )
        return tuple((await self._session.scalars(statement)).all())

    async def ensure_balance(self, business_id: int, product_id: int, business_date: date) -> None:
        statement = (
            pg_insert(InventoryBalance)
            .values(
                business_id=business_id,
                product_id=product_id,
                business_date=business_date,
                on_hand_qty=Decimal(0),
                reserved_qty=Decimal(0),
                version=1,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    InventoryBalance.business_id,
                    InventoryBalance.product_id,
                    InventoryBalance.business_date,
                ]
            )
        )
        await self._session.execute(statement)

    async def lock_balances(
        self,
        business_id: int,
        product_ids: Sequence[int],
        business_date: date,
    ) -> tuple[InventoryBalance, ...]:
        ids = sorted(set(product_ids))
        statement = (
            select(InventoryBalance)
            .where(
                InventoryBalance.business_id == business_id,
                InventoryBalance.product_id.in_(ids),
                InventoryBalance.business_date == business_date,
            )
            .order_by(InventoryBalance.product_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return tuple((await self._session.scalars(statement)).all())

    async def get_balances(
        self,
        business_id: int,
        product_ids: Sequence[int],
        business_date: date,
    ) -> tuple[InventoryBalance, ...]:
        ids = sorted(set(product_ids))
        statement = (
            select(InventoryBalance)
            .where(
                InventoryBalance.business_id == business_id,
                InventoryBalance.product_id.in_(ids),
                InventoryBalance.business_date == business_date,
            )
            .order_by(InventoryBalance.product_id)
        )
        return tuple((await self._session.scalars(statement)).all())

    async def update_balance(
        self,
        *,
        balance_id: int,
        business_id: int,
        expected_version: int,
        on_hand_qty: Decimal,
        reserved_qty: Decimal,
    ) -> InventoryBalance | None:
        statement = (
            update(InventoryBalance)
            .where(
                InventoryBalance.id == balance_id,
                InventoryBalance.business_id == business_id,
                InventoryBalance.version == expected_version,
            )
            .values(
                on_hand_qty=on_hand_qty,
                reserved_qty=reserved_qty,
                version=InventoryBalance.version + 1,
                updated_at=func.now(),
            )
            .returning(InventoryBalance)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def insert_movement(self, values: Mapping[str, Any]) -> InventoryMovement:
        movement = InventoryMovement(**values)
        self._session.add(movement)
        await self._session.flush()
        return movement

    async def get_movement_for_pending_action(
        self, business_id: int, pending_action_id: int
    ) -> InventoryMovement | None:
        statement = (
            select(InventoryMovement)
            .where(
                InventoryMovement.business_id == business_id,
                InventoryMovement.pending_action_id == pending_action_id,
            )
            .order_by(InventoryMovement.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(statement)).first()

    async def lock_active_reservations(
        self, business_id: int, order_id: int
    ) -> tuple[InventoryReservation, ...]:
        statement = (
            select(InventoryReservation)
            .where(
                InventoryReservation.business_id == business_id,
                InventoryReservation.order_id == order_id,
                InventoryReservation.status == InventoryReservationStatus.ACTIVE.value,
            )
            .order_by(
                InventoryReservation.business_date,
                InventoryReservation.product_id,
                InventoryReservation.id,
            )
            .with_for_update()
        )
        return tuple((await self._session.scalars(statement)).all())

    async def lock_due_reservations(
        self, now: datetime, batch_size: int
    ) -> tuple[InventoryReservation, ...]:
        statement = (
            select(InventoryReservation)
            .where(
                InventoryReservation.status == InventoryReservationStatus.ACTIVE.value,
                InventoryReservation.expires_at <= now,
            )
            .order_by(
                InventoryReservation.expires_at,
                InventoryReservation.business_id,
                InventoryReservation.product_id,
                InventoryReservation.id,
            )
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        return tuple((await self._session.scalars(statement)).all())

    async def count_active_reservations(self, business_id: int, order_id: int) -> int:
        value = await self._session.scalar(
            select(func.count(InventoryReservation.id)).where(
                InventoryReservation.business_id == business_id,
                InventoryReservation.order_id == order_id,
                InventoryReservation.status == InventoryReservationStatus.ACTIVE.value,
            )
        )
        return int(value or 0)

    async def get_all_balances(
        self, business_id: int, product_id: int | None = None
    ) -> tuple[InventoryBalance, ...]:
        statement = select(InventoryBalance).where(InventoryBalance.business_id == business_id)
        if product_id is not None:
            statement = statement.where(InventoryBalance.product_id == product_id)
        statement = statement.order_by(InventoryBalance.product_id, InventoryBalance.business_date)
        return tuple((await self._session.scalars(statement)).all())

    async def get_operation_by_key(
        self, business_id: int, idempotency_key: str
    ) -> InventoryOperation | None:
        statement = (
            select(InventoryOperation)
            .where(
                InventoryOperation.business_id == business_id,
                InventoryOperation.idempotency_key == idempotency_key,
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def insert_operation(self, values: Mapping[str, Any]) -> InventoryOperation:
        operation = InventoryOperation(**values)
        self._session.add(operation)
        await self._session.flush()
        return operation

    async def get_movement_by_id(
        self, business_id: int, movement_id: int
    ) -> InventoryMovement | None:
        statement = (
            select(InventoryMovement)
            .where(
                InventoryMovement.business_id == business_id,
                InventoryMovement.id == movement_id,
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def ledger_totals(self, business_id: int) -> Sequence[Any]:
        statement: Select[tuple[Any, ...]] = (
            select(
                InventoryMovement.product_id,
                InventoryMovement.business_date,
                func.coalesce(func.sum(InventoryMovement.on_hand_delta), 0),
                func.coalesce(func.sum(InventoryMovement.reserved_delta), 0),
            )
            .where(InventoryMovement.business_id == business_id)
            .group_by(InventoryMovement.product_id, InventoryMovement.business_date)
            .order_by(InventoryMovement.product_id, InventoryMovement.business_date)
        )
        return cast(Sequence[Any], (await self._session.execute(statement)).all())
