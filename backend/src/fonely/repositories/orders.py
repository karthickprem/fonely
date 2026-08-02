"""Tenant-scoped order persistence within a caller-owned transaction."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.enums import InventoryReservationStatus, OrderStatus
from fonely.models.schema import InventoryReservation, Order, OrderLineItem


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, business_id: int, order_id: int) -> Order | None:
        statement = (
            select(Order)
            .where(Order.business_id == business_id, Order.id == order_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def lock_by_id(self, business_id: int, order_id: int) -> Order | None:
        statement = (
            select(Order)
            .where(Order.business_id == business_id, Order.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def lock_global_by_id(self, order_id: int) -> Order | None:
        statement = (
            select(Order)
            .where(Order.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def get_by_idempotency_key(self, business_id: int, key: str) -> Order | None:
        statement = (
            select(Order)
            .where(Order.business_id == business_id, Order.idempotency_key == key)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def get_lines(self, business_id: int, order_id: int) -> tuple[OrderLineItem, ...]:
        statement = (
            select(OrderLineItem)
            .join(Order, Order.id == OrderLineItem.order_id)
            .where(Order.business_id == business_id, OrderLineItem.order_id == order_id)
            .order_by(OrderLineItem.product_id)
        )
        return tuple((await self._session.scalars(statement)).all())

    async def get_reservation_expiries(
        self, business_id: int, order_id: int
    ) -> tuple[datetime, ...]:
        statement = (
            select(InventoryReservation.expires_at)
            .where(
                InventoryReservation.business_id == business_id,
                InventoryReservation.order_id == order_id,
            )
            .order_by(InventoryReservation.expires_at, InventoryReservation.id)
        )
        return tuple((await self._session.scalars(statement)).all())

    async def get_reservation_expiry(self, business_id: int, order_id: int) -> datetime | None:
        expiries = await self.get_reservation_expiries(business_id, order_id)
        return expiries[0] if expiries else None

    async def get_by_pending_action(self, business_id: int, pending_action_id: int) -> Order | None:
        statement = (
            select(Order)
            .where(
                Order.business_id == business_id,
                Order.pending_action_id == pending_action_id,
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def insert_idempotent(self, values: Mapping[str, Any]) -> Order | None:
        statement = (
            pg_insert(Order)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[Order.business_id, Order.idempotency_key])
            .returning(Order)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def insert_lines(self, values: Sequence[Mapping[str, Any]]) -> tuple[OrderLineItem, ...]:
        lines = tuple(OrderLineItem(**value) for value in values)
        self._session.add_all(lines)
        await self._session.flush()
        return lines

    async def insert_reservations(
        self, values: Sequence[Mapping[str, Any]]
    ) -> tuple[InventoryReservation, ...]:
        reservations = tuple(InventoryReservation(**value) for value in values)
        self._session.add_all(reservations)
        await self._session.flush()
        return reservations

    async def update_status(
        self,
        business_id: int,
        order_id: int,
        expected_status: str,
        new_status: str,
    ) -> Order | None:
        statement = (
            update(Order)
            .where(
                Order.business_id == business_id,
                Order.id == order_id,
                Order.status == expected_status,
            )
            .values(status=new_status)
            .returning(Order)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def mark_reservation_terminal(
        self,
        *,
        business_id: int,
        reservation_id: int,
        expected_status: str,
        status: str,
        released_at: datetime,
    ) -> InventoryReservation | None:
        statement = (
            update(InventoryReservation)
            .where(
                InventoryReservation.business_id == business_id,
                InventoryReservation.id == reservation_id,
                InventoryReservation.status == expected_status,
            )
            .values(status=status, released_at=released_at)
            .returning(InventoryReservation)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def find_due_order_ids(self, now: datetime, batch_size: int) -> tuple[int, ...]:
        expiry_by_order = (
            select(
                InventoryReservation.order_id.label("order_id"),
                func.min(InventoryReservation.expires_at).label("expires_at"),
            )
            .where(
                InventoryReservation.status == InventoryReservationStatus.ACTIVE.value,
                InventoryReservation.order_id.isnot(None),
            )
            .group_by(InventoryReservation.order_id)
            .subquery()
        )
        statement = (
            select(Order.id)
            .join(expiry_by_order, expiry_by_order.c.order_id == Order.id)
            .where(
                Order.status == OrderStatus.CONFIRMED.value,
                expiry_by_order.c.expires_at <= now,
            )
            .order_by(expiry_by_order.c.expires_at, Order.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True, of=Order)
        )
        return tuple((await self._session.scalars(statement)).all())
