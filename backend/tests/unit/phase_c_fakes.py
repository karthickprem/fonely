"""Stateful Phase C service-test infrastructure with savepoint rollback."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from fonely.domain.pending_actions.commands import (
    BeginCommitCommand,
    CompleteCommitCommand,
    FailCommitCommand,
    InternalGetPendingActionQuery,
)
from fonely.domain.pending_actions.results import PendingActionResult
from fonely.models.enums import (
    InventoryReservationStatus,
    OrderStatus,
    PendingActionStatus,
)


@dataclass
class FakePhaseCState:
    products: dict[tuple[int, int], SimpleNamespace] = field(default_factory=dict)
    balances: dict[tuple[int, date, int], SimpleNamespace] = field(default_factory=dict)
    orders: dict[int, SimpleNamespace] = field(default_factory=dict)
    lines: dict[int, list[SimpleNamespace]] = field(default_factory=dict)
    reservations: dict[int, SimpleNamespace] = field(default_factory=dict)
    movements: dict[int, SimpleNamespace] = field(default_factory=dict)
    operations: dict[tuple[int, str], SimpleNamespace] = field(default_factory=dict)
    pending_actions: dict[int, PendingActionResult] = field(default_factory=dict)
    timezones: dict[int, str] = field(default_factory=lambda: {1: "Asia/Kolkata"})
    events: list[str] = field(default_factory=list)
    fail_at: str | None = None
    next_order_id: int = 1
    next_line_id: int = 1
    next_reservation_id: int = 1
    next_movement_id: int = 1
    next_balance_id: int = 1
    next_operation_id: int = 1

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "products": self.products,
                "balances": self.balances,
                "orders": self.orders,
                "lines": self.lines,
                "reservations": self.reservations,
                "movements": self.movements,
                "operations": self.operations,
                "pending_actions": self.pending_actions,
                "timezones": self.timezones,
                "next_order_id": self.next_order_id,
                "next_line_id": self.next_line_id,
                "next_reservation_id": self.next_reservation_id,
                "next_movement_id": self.next_movement_id,
                "next_balance_id": self.next_balance_id,
                "next_operation_id": self.next_operation_id,
            }
        )

    def restore(self, snapshot: dict[str, Any]) -> None:
        for name, value in snapshot.items():
            setattr(self, name, value)

    def inject(self, stage: str) -> None:
        self.events.append(stage)
        if self.fail_at == stage:
            raise InjectedFailureError(stage)


class InjectedFailureError(SQLAlchemyError):
    pass


class FakeNestedTransaction:
    def __init__(self, state: FakePhaseCState) -> None:
        self.state = state
        self.before: dict[str, Any] | None = None

    async def __aenter__(self) -> FakeNestedTransaction:
        self.before = self.state.snapshot()
        self.state.events.append("savepoint:enter")
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if exc_type is not None:
            assert self.before is not None
            self.state.restore(self.before)
            self.state.events.append("savepoint:rollback")
        else:
            self.state.events.append("savepoint:release")
        return False


class FakeSession:
    def __init__(self, state: FakePhaseCState) -> None:
        self.state = state

    def begin_nested(self) -> FakeNestedTransaction:
        return FakeNestedTransaction(self.state)


class FakeInventoryRepository:
    def __init__(self, state: FakePhaseCState) -> None:
        self.state = state

    async def get_business_timezone(self, business_id: int) -> str | None:
        return self.state.timezones.get(business_id)

    async def get_active_products(
        self, business_id: int, product_ids: tuple[int, ...] | list[int]
    ) -> tuple[SimpleNamespace, ...]:
        ids = (
            sorted(set(product_ids))
            if product_ids
            else sorted(
                product_id for tenant, product_id in self.state.products if tenant == business_id
            )
        )
        return tuple(
            product
            for product_id in ids
            if (product := self.state.products.get((business_id, product_id))) is not None
            and product.is_active
        )

    async def lock_active_products(
        self, business_id: int, product_ids: tuple[int, ...] | list[int]
    ) -> tuple[SimpleNamespace, ...]:
        ids = sorted(set(product_ids))
        self.state.events.append(f"lock:products:{business_id}:{ids}")
        return await self.get_active_products(business_id, ids)

    async def ensure_balance(self, business_id: int, product_id: int, business_date: date) -> None:
        key = (business_id, business_date, product_id)
        if key not in self.state.balances:
            balance_id = self.state.next_balance_id
            self.state.next_balance_id += 1
            self.state.balances[key] = SimpleNamespace(
                id=balance_id,
                business_id=business_id,
                product_id=product_id,
                business_date=business_date,
                on_hand_qty=Decimal(0),
                reserved_qty=Decimal(0),
                version=1,
                available_qty=Decimal(0),
            )

    async def lock_balances(
        self, business_id: int, product_ids: tuple[int, ...] | list[int], business_date: date
    ) -> tuple[SimpleNamespace, ...]:
        ids = sorted(set(product_ids))
        self.state.events.append(f"lock:balances:{business_id}:{business_date.isoformat()}:{ids}")
        return tuple(
            balance
            for product_id in ids
            if (balance := self.state.balances.get((business_id, business_date, product_id)))
            is not None
        )

    async def get_balances(
        self, business_id: int, product_ids: tuple[int, ...] | list[int], business_date: date
    ) -> tuple[SimpleNamespace, ...]:
        return await self.lock_balances(business_id, product_ids, business_date)

    async def update_balance(
        self,
        *,
        balance_id: int,
        business_id: int,
        expected_version: int,
        on_hand_qty: Decimal,
        reserved_qty: Decimal,
    ) -> SimpleNamespace | None:
        self.state.inject("balance:update")
        for balance in self.state.balances.values():
            if balance.id == balance_id and balance.business_id == business_id:
                if balance.version != expected_version:
                    return None
                balance.on_hand_qty = on_hand_qty
                balance.reserved_qty = reserved_qty
                balance.available_qty = on_hand_qty - reserved_qty
                balance.version += 1
                return balance
        return None

    async def insert_movement(self, values: dict[str, Any]) -> SimpleNamespace:
        self.state.inject("movement:insert")
        movement_id = self.state.next_movement_id
        self.state.next_movement_id += 1
        movement = SimpleNamespace(id=movement_id, **values)
        self.state.movements[movement_id] = movement
        return movement

    async def get_movement_for_pending_action(
        self, business_id: int, pending_action_id: int
    ) -> SimpleNamespace | None:
        return next(
            (
                movement
                for movement in self.state.movements.values()
                if movement.business_id == business_id
                and getattr(movement, "pending_action_id", None) == pending_action_id
            ),
            None,
        )

    async def lock_active_reservations(
        self, business_id: int, order_id: int
    ) -> tuple[SimpleNamespace, ...]:
        rows = tuple(
            sorted(
                (
                    row
                    for row in self.state.reservations.values()
                    if row.business_id == business_id
                    and row.order_id == order_id
                    and row.status == InventoryReservationStatus.ACTIVE.value
                ),
                key=lambda row: (row.product_id, row.id),
            )
        )
        self.state.events.append(
            f"lock:reservations:{business_id}:{order_id}:{[row.id for row in rows]}"
        )
        return rows

    async def count_active_reservations(self, business_id: int, order_id: int) -> int:
        return sum(
            row.business_id == business_id
            and row.order_id == order_id
            and row.status == InventoryReservationStatus.ACTIVE.value
            for row in self.state.reservations.values()
        )

    async def get_all_balances(
        self, business_id: int, product_id: int | None = None
    ) -> tuple[SimpleNamespace, ...]:
        return tuple(
            balance
            for (tenant, _, product), balance in sorted(self.state.balances.items())
            if tenant == business_id and (product_id is None or product == product_id)
        )

    async def get_operation_by_key(
        self, business_id: int, idempotency_key: str
    ) -> SimpleNamespace | None:
        return self.state.operations.get((business_id, idempotency_key))

    async def insert_operation(self, values: dict[str, Any]) -> SimpleNamespace:
        op_id = self.state.next_operation_id
        self.state.next_operation_id += 1
        operation = SimpleNamespace(id=op_id, **values)
        key = (values["business_id"], values["idempotency_key"])
        self.state.operations[key] = operation
        return operation

    async def get_movement_by_id(
        self, business_id: int, movement_id: int
    ) -> SimpleNamespace | None:
        movement = self.state.movements.get(movement_id)
        if movement is not None and movement.business_id == business_id:
            return movement
        return None

    async def ledger_totals(self, business_id: int) -> tuple[tuple[Any, ...], ...]:
        totals: dict[tuple[int, date], list[Decimal]] = {}
        for movement in self.state.movements.values():
            if movement.business_id != business_id:
                continue
            values = totals.setdefault(
                (movement.product_id, movement.business_date), [Decimal(0), Decimal(0)]
            )
            values[0] += movement.on_hand_delta
            values[1] += movement.reserved_delta
        return tuple(
            (product_id, business_date, values[0], values[1])
            for (product_id, business_date), values in sorted(totals.items())
        )


class FakeOrderRepository:
    def __init__(self, state: FakePhaseCState) -> None:
        self.state = state

    async def get_by_id(self, business_id: int, order_id: int) -> SimpleNamespace | None:
        order = self.state.orders.get(order_id)
        return order if order is not None and order.business_id == business_id else None

    async def lock_by_id(self, business_id: int, order_id: int) -> SimpleNamespace | None:
        self.state.events.append(f"lock:order:{business_id}:{order_id}")
        return await self.get_by_id(business_id, order_id)

    async def lock_global_by_id(self, order_id: int) -> SimpleNamespace | None:
        self.state.events.append(f"lock:order-global:{order_id}")
        return self.state.orders.get(order_id)

    async def get_by_idempotency_key(self, business_id: int, key: str) -> SimpleNamespace | None:
        return next(
            (
                order
                for order in self.state.orders.values()
                if order.business_id == business_id and order.idempotency_key == key
            ),
            None,
        )

    async def get_by_pending_action(
        self, business_id: int, pending_action_id: int
    ) -> SimpleNamespace | None:
        return next(
            (
                order
                for order in self.state.orders.values()
                if order.business_id == business_id and order.pending_action_id == pending_action_id
            ),
            None,
        )

    async def insert_idempotent(self, values: dict[str, Any]) -> SimpleNamespace | None:
        self.state.inject("order:insert")
        if await self.get_by_idempotency_key(values["business_id"], values["idempotency_key"]):
            return None
        order_id = self.state.next_order_id
        self.state.next_order_id += 1
        order = SimpleNamespace(id=order_id, **values)
        self.state.orders[order_id] = order
        return order

    async def insert_lines(self, values: list[dict[str, Any]]) -> tuple[SimpleNamespace, ...]:
        rows = []
        for value in values:
            line_id = self.state.next_line_id
            self.state.next_line_id += 1
            rows.append(SimpleNamespace(id=line_id, **value))
        self.state.lines.setdefault(values[0]["order_id"], []).extend(rows)
        self.state.inject("lines:insert")
        return tuple(rows)

    async def insert_reservations(
        self, values: list[dict[str, Any]]
    ) -> tuple[SimpleNamespace, ...]:
        rows = []
        for value in values:
            reservation_id = self.state.next_reservation_id
            self.state.next_reservation_id += 1
            row = SimpleNamespace(id=reservation_id, released_at=None, **value)
            self.state.reservations[reservation_id] = row
            rows.append(row)
        self.state.inject("reservations:insert")
        return tuple(rows)

    async def get_lines(self, business_id: int, order_id: int) -> tuple[SimpleNamespace, ...]:
        if await self.get_by_id(business_id, order_id) is None:
            return ()
        return tuple(sorted(self.state.lines.get(order_id, []), key=lambda row: row.product_id))

    async def get_reservation_expiries(
        self, business_id: int, order_id: int
    ) -> tuple[datetime, ...]:
        return tuple(
            row.expires_at
            for row in sorted(self.state.reservations.values(), key=lambda row: row.id)
            if row.business_id == business_id and row.order_id == order_id
        )

    async def update_status(
        self,
        business_id: int,
        order_id: int,
        expected_status: str,
        new_status: str,
    ) -> SimpleNamespace | None:
        self.state.inject("order:status")
        order = await self.get_by_id(business_id, order_id)
        if order is None or order.status != expected_status:
            return None
        order.status = new_status
        return order

    async def mark_reservation_terminal(
        self,
        *,
        business_id: int,
        reservation_id: int,
        expected_status: str,
        status: str,
        released_at: datetime,
    ) -> SimpleNamespace | None:
        self.state.inject("reservation:terminal")
        row = self.state.reservations.get(reservation_id)
        if row is None or row.business_id != business_id or row.status != expected_status:
            return None
        row.status = status
        row.released_at = released_at
        return row

    async def find_due_order_ids(self, now: datetime, batch_size: int) -> tuple[int, ...]:
        candidates = []
        for order in self.state.orders.values():
            if order.status != OrderStatus.CONFIRMED.value:
                continue
            expiries = [
                row.expires_at
                for row in self.state.reservations.values()
                if row.order_id == order.id
                and row.status == InventoryReservationStatus.ACTIVE.value
            ]
            if expiries and min(expiries) <= now:
                candidates.append((min(expiries), order.id))
        return tuple(order_id for _, order_id in sorted(candidates)[:batch_size])


class FakePendingActionService:
    def __init__(self, state: FakePhaseCState) -> None:
        self.state = state

    async def internal_get(self, query: InternalGetPendingActionQuery) -> PendingActionResult:
        action = self.state.pending_actions[query.action_id]
        if action.business_id != query.business_id:
            raise KeyError(query.action_id)
        return action

    async def begin_commit(self, command: BeginCommitCommand) -> PendingActionResult:
        action = self.state.pending_actions[command.context.pending_action_id]
        if (
            action.version != command.context.expected_version
            or action.status is not PendingActionStatus.AWAITING_CONFIRMATION
        ):
            raise RuntimeError("stale pending action")
        updated = action.model_copy(
            update={"status": PendingActionStatus.COMMITTING, "version": action.version + 1}
        )
        self.state.pending_actions[action.id] = updated
        self.state.events.append("pending:begin")
        return updated

    async def complete_commit(self, command: CompleteCommitCommand) -> PendingActionResult:
        self.state.inject("pending:complete")
        action = self.state.pending_actions[command.context.pending_action_id]
        if (
            action.version != command.context.expected_version
            or action.status is not PendingActionStatus.COMMITTING
        ):
            raise RuntimeError("stale pending completion")
        if command.committed_entity_type == "order":
            entity = self.state.orders.get(command.committed_entity_id)
        else:
            entity = self.state.movements.get(command.committed_entity_id)
        if entity is None or getattr(entity, "pending_action_id", None) != action.id:
            raise RuntimeError("committed entity missing")
        updated = action.model_copy(
            update={
                "status": PendingActionStatus.CONFIRMED,
                "version": action.version + 1,
                "committed_entity_type": command.committed_entity_type,
                "committed_entity_id": command.committed_entity_id,
            }
        )
        self.state.pending_actions[action.id] = updated
        return updated

    async def fail_commit(self, command: FailCommitCommand) -> PendingActionResult:
        action = self.state.pending_actions[command.context.pending_action_id]
        if (
            action.version != command.context.expected_version
            or action.status is not PendingActionStatus.COMMITTING
        ):
            raise RuntimeError("stale pending failure")
        status = (
            PendingActionStatus.AWAITING_CONFIRMATION
            if command.retryable
            else PendingActionStatus.REJECTED
        )
        updated = action.model_copy(
            update={
                "status": status,
                "version": action.version + 1,
                "error_code": command.error_code,
            }
        )
        self.state.pending_actions[action.id] = updated
        self.state.events.append(f"pending:fail:{command.error_code}")
        return updated
