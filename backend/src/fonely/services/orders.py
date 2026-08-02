"""Deterministic order transactions within a caller-owned session transaction."""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.exceptions import FonelyError
from fonely.domain.inventory.calculations import (
    InventoryState,
    InventoryTransition,
    complete_pickup,
    release_reservation,
    reserve_stock,
)
from fonely.domain.inventory.errors import (
    InsufficientAvailableStockError,
    InvalidProductError,
    InventoryBalanceNotFoundError,
    InventoryStaleVersionError,
)
from fonely.domain.inventory.policies import derive_business_date
from fonely.domain.orders.calculations import AuthoritativeProduct, price_order_lines
from fonely.domain.orders.commands import (
    CancelOrderCommand,
    CompletePickupCommand,
    ConfirmPendingOrderCommand,
    ExpireOrderReservationsCommand,
    GetOrderQuery,
)
from fonely.domain.orders.errors import (
    OrderIdempotencyConflictError,
    OrderNotFoundError,
    OrderUnauthorizedError,
)
from fonely.domain.orders.policies import require_cancellable, require_pickup
from fonely.domain.orders.results import OrderExpiryResult, OrderLineResult, OrderResult
from fonely.domain.pending_actions.commands import (
    ActorContext,
    BeginCommitCommand,
    CommitResultContext,
    CompleteCommitCommand,
    FailCommitCommand,
    InternalGetPendingActionQuery,
)
from fonely.domain.pending_actions.results import PendingActionResult
from fonely.models.enums import (
    CallerRole,
    InventoryMovementType,
    InventoryReservationStatus,
    OrderStatus,
    PendingActionStatus,
    ProductUnit,
)
from fonely.models.schema import InventoryBalance, InventoryReservation, Order, OrderLineItem
from fonely.repositories.inventory import InventoryRepository
from fonely.repositories.orders import OrderRepository
from fonely.services.authorization import require_owner_or_manager
from fonely.services.pending_actions import PendingActionService


class OrderService:
    """Coordinates order policy without committing or rolling back the session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._inventory = InventoryRepository(session)
        self._orders = OrderRepository(session)
        self._pending = PendingActionService(session)

    async def get(self, query: GetOrderQuery) -> OrderResult:
        order = await self._require_order(query.actor.business_id, query.order_id)
        await self._authorize_order_read(order, query.actor)
        return await self._complete_result(order)

    async def confirm(self, command: ConfirmPendingOrderCommand) -> OrderResult:
        pending = await self._pending.internal_get(
            InternalGetPendingActionQuery(
                business_id=command.context.business_id,
                action_id=command.context.pending_action_id,
            )
        )
        await self._authorize_order_actor(command.actor, pending.payload)
        self._assert_confirmed_command(command, pending)

        existing = await self._orders.get_by_pending_action(
            command.context.business_id, command.context.pending_action_id
        )
        if existing is not None:
            return await self._replay_order(existing, command, pending)

        committing = await self._pending.begin_commit(BeginCommitCommand(context=command.context))
        commit_context = command.context.model_copy(update={"expected_version": committing.version})

        try:
            async with self._session.begin_nested():
                result, completion_needed = await self._create_confirmed_order(command)
                if completion_needed:
                    await self._pending.complete_commit(
                        CompleteCommitCommand(
                            context=commit_context,
                            committed_entity_type="order",
                            committed_entity_id=result.id,
                        )
                    )
                return result
        except InsufficientAvailableStockError:
            await self._fail_commit(commit_context, "insufficient_stock", retryable=True)
            raise
        except InvalidProductError:
            await self._fail_commit(commit_context, "invalid_product", retryable=False)
            raise
        except InventoryStaleVersionError:
            await self._fail_commit(commit_context, "temporary_conflict", retryable=True)
            raise
        except (FonelyError, SQLAlchemyError):
            await self._fail_commit(commit_context, "transaction_failed", retryable=False)
            raise

    async def cancel(self, command: CancelOrderCommand) -> OrderResult:
        async with self._session.begin_nested():
            order = await self._require_locked_order(command.actor.business_id, command.order_id)
            await self._authorize_cancellation(
                order,
                command.actor.normalized_phone,
                command.actor.verified_role,
                command.actor,
            )
            if not require_cancellable(OrderStatus(order.status)):
                return await self._complete_result(order, replay=True)

            reservations = await self._inventory.lock_active_reservations(
                order.business_id, order.id
            )
            await self._release_reservations(
                reservations, command.now, InventoryMovementType.ORDER_CANCELLED
            )
            updated = await self._orders.update_status(
                order.business_id,
                order.id,
                OrderStatus.CONFIRMED.value,
                OrderStatus.CANCELLED.value,
            )
            if updated is None:
                raise InventoryStaleVersionError("Order changed during cancellation")
            return await self._complete_result(updated)

    async def complete_pickup(self, command: CompletePickupCommand) -> OrderResult:
        async with self._session.begin_nested():
            order = await self._require_locked_order(command.actor.business_id, command.order_id)
            await require_owner_or_manager(self._session, command.actor)
            status = OrderStatus(order.status)
            if status is OrderStatus.PICKED_UP:
                return await self._complete_result(order, replay=True)
            if status is OrderStatus.CANCELLED:
                require_pickup(status, command.now, command.now)
            reservations = await self._inventory.lock_active_reservations(
                order.business_id, order.id
            )
            expires_at = self._one_reservation_expiry(reservations)
            require_pickup(status, expires_at, command.now)

            await self._consume_reservations(reservations, command.now)
            updated = await self._orders.update_status(
                order.business_id,
                order.id,
                OrderStatus.CONFIRMED.value,
                OrderStatus.PICKED_UP.value,
            )
            if updated is None:
                raise InventoryStaleVersionError("Order changed during pickup")
            return await self._complete_result(updated)

    async def expire(self, command: ExpireOrderReservationsCommand) -> OrderExpiryResult:
        due_order_ids = await self._orders.find_due_order_ids(command.now, command.batch_size)
        expired_order_ids: list[int] = []
        expired_reservation_ids: list[int] = []

        for order_id in due_order_ids:
            async with self._session.begin_nested():
                order = await self._orders.lock_global_by_id(int(order_id))
                if order is None or order.status != OrderStatus.CONFIRMED.value:
                    continue
                reservations = await self._inventory.lock_active_reservations(
                    order.business_id, order.id
                )
                if not reservations:
                    continue
                expires_at = self._one_reservation_expiry(reservations)
                if expires_at > command.now:
                    raise OrderIdempotencyConflictError(
                        "Due-order selection disagrees with reservation expiry"
                    )

                await self._release_reservations(
                    reservations, command.now, InventoryMovementType.RESERVATION_RELEASED
                )
                if await self._inventory.count_active_reservations(order.business_id, order.id):
                    raise InventoryStaleVersionError(
                        "Active reservations remain after expiry release"
                    )
                updated = await self._orders.update_status(
                    order.business_id,
                    order.id,
                    OrderStatus.CONFIRMED.value,
                    OrderStatus.CANCELLED.value,
                )
                if updated is None:
                    raise InventoryStaleVersionError("Order changed during expiry")
                expired_order_ids.append(order.id)
                expired_reservation_ids.extend(row.id for row in reservations)

        return OrderExpiryResult(
            expired_order_ids=tuple(expired_order_ids),
            expired_reservation_ids=tuple(expired_reservation_ids),
            count=len(expired_reservation_ids),
        )

    async def _create_confirmed_order(
        self,
        command: ConfirmPendingOrderCommand,
    ) -> tuple[OrderResult, bool]:
        payload = await self._pending.internal_get(
            InternalGetPendingActionQuery(
                business_id=command.context.business_id,
                action_id=command.context.pending_action_id,
            )
        )
        payload_data = payload.payload["data"]
        assert isinstance(payload_data, dict)
        product_ids = [line.product_id for line in command.lines]
        products = await self._inventory.lock_active_products(
            command.context.business_id, product_ids
        )
        if len(products) != len(product_ids):
            raise InvalidProductError("One or more products are unavailable")

        timezone = await self._require_timezone(command.context.business_id)
        business_date = derive_business_date(command.now, timezone)
        for product_id in product_ids:
            await self._inventory.ensure_balance(
                command.context.business_id, product_id, business_date
            )
        balances = await self._inventory.lock_balances(
            command.context.business_id, product_ids, business_date
        )
        if len(balances) != len(product_ids):
            raise InventoryBalanceNotFoundError("One or more inventory balances are unavailable")

        quantities = {line.product_id: line.quantity for line in command.lines}
        balance_by_product = {balance.product_id: balance for balance in balances}
        transitions: dict[int, InventoryTransition] = {}
        shortages: list[int] = []
        for product_id in product_ids:
            balance = balance_by_product[product_id]
            try:
                transitions[product_id] = reserve_stock(
                    InventoryState(balance.on_hand_qty, balance.reserved_qty),
                    quantities[product_id],
                )
            except InsufficientAvailableStockError:
                shortages.append(product_id)
        if shortages:
            raise InsufficientAvailableStockError(f"Insufficient stock for products: {shortages}")

        pricing = price_order_lines(
            quantities,
            {
                product.id: AuthoritativeProduct(
                    product.id,
                    product.name,
                    ProductUnit(product.unit),
                    product.price_per_unit,
                )
                for product in products
            },
        )
        order = await self._orders.insert_idempotent(
            {
                "business_id": command.context.business_id,
                "customer_name": payload_data.get("customer_name"),
                "customer_phone": str(payload_data["customer_phone"]),
                "total_amount": pricing.total,
                "pickup_at": datetime.fromisoformat(str(payload_data["pickup_at"])),
                "status": OrderStatus.CONFIRMED.value,
                "idempotency_key": command.idempotency_key,
                "pending_action_id": command.context.pending_action_id,
            }
        )
        if order is None:
            winner = await self._orders.get_by_idempotency_key(
                command.context.business_id, command.idempotency_key
            )
            if winner is None or winner.pending_action_id != command.context.pending_action_id:
                raise OrderIdempotencyConflictError("Order idempotency key conflicts")
            lines, expiry = await self._assert_order_semantics(winner, command)
            return self._to_result(winner, lines, expiry, replay=True), True

        lines = await self._orders.insert_lines(
            [
                {
                    "order_id": order.id,
                    "product_id": line.product_id,
                    "product_name_snapshot": line.product_name,
                    "qty": line.quantity,
                    "unit": line.unit.value,
                    "price_per_unit_snapshot": line.price_per_unit,
                    "subtotal": line.subtotal,
                }
                for line in pricing.lines
            ]
        )
        reservations = await self._orders.insert_reservations(
            [
                {
                    "business_id": command.context.business_id,
                    "product_id": line.product_id,
                    "pending_action_id": command.context.pending_action_id,
                    "order_id": order.id,
                    "business_date": business_date,
                    "qty": line.quantity,
                    "status": InventoryReservationStatus.ACTIVE.value,
                    "expires_at": command.reservation_expires_at,
                    "idempotency_key": command.idempotency_key,
                }
                for line in command.lines
            ]
        )
        self._one_reservation_expiry(reservations)
        reservation_by_product = {row.product_id: row for row in reservations}
        for product_id in product_ids:
            balance = balance_by_product[product_id]
            transition = transitions[product_id]
            updated = await self._inventory.update_balance(
                balance_id=balance.id,
                business_id=command.context.business_id,
                expected_version=balance.version,
                on_hand_qty=transition.after.on_hand,
                reserved_qty=transition.after.reserved,
            )
            if updated is None:
                raise InventoryStaleVersionError("Inventory balance changed concurrently")
            reservation = reservation_by_product[product_id]
            await self._inventory.insert_movement(
                {
                    "business_id": command.context.business_id,
                    "product_id": product_id,
                    "business_date": business_date,
                    "movement_type": InventoryMovementType.PHONE_ORDER_RESERVED.value,
                    "on_hand_delta": transition.on_hand_delta,
                    "reserved_delta": transition.reserved_delta,
                    "on_hand_after": transition.after.on_hand,
                    "reserved_after": transition.after.reserved,
                    "available_after": transition.after.available,
                    "order_id": order.id,
                    "reservation_id": reservation.id,
                    "pending_action_id": command.context.pending_action_id,
                    "initiated_by": command.actor.normalized_phone,
                }
            )
        return self._to_result(order, lines, command.reservation_expires_at), True

    async def _replay_order(
        self,
        existing: Order,
        command: ConfirmPendingOrderCommand,
        pending: PendingActionResult,
    ) -> OrderResult:
        lines, expiry = await self._assert_order_semantics(existing, command)
        if pending.status is PendingActionStatus.CONFIRMED:
            if (
                pending.committed_entity_type != "order"
                or pending.committed_entity_id != existing.id
            ):
                raise OrderIdempotencyConflictError(
                    "PendingAction is confirmed with different evidence"
                )
            return self._to_result(existing, lines, expiry, replay=True)
        if pending.status is PendingActionStatus.COMMITTING:
            repair_context = command.context.model_copy(
                update={"expected_version": pending.version}
            )
            try:
                async with self._session.begin_nested():
                    await self._pending.complete_commit(
                        CompleteCommitCommand(
                            context=repair_context,
                            committed_entity_type="order",
                            committed_entity_id=existing.id,
                        )
                    )
            except (FonelyError, SQLAlchemyError):
                await self._fail_commit(repair_context, "transaction_failed", retryable=False)
                raise
            return self._to_result(existing, lines, expiry, replay=True)
        raise OrderIdempotencyConflictError(
            f"Committed order evidence conflicts with PendingAction state {pending.status.value}"
        )

    async def _assert_order_semantics(
        self, existing: Order, command: ConfirmPendingOrderCommand
    ) -> tuple[tuple[OrderLineItem, ...], datetime]:
        lines = await self._orders.get_lines(existing.business_id, existing.id)
        expiries = await self._orders.get_reservation_expiries(existing.business_id, existing.id)
        expiry = self._one_expiry(expiries)
        existing_lines = tuple(
            (line.product_id, line.qty) for line in sorted(lines, key=lambda row: row.product_id)
        )
        requested_lines = tuple((line.product_id, line.quantity) for line in command.lines)
        if existing_lines != requested_lines:
            raise OrderIdempotencyConflictError(
                "Replay lines do not match existing confirmed order"
            )
        if existing.idempotency_key != command.idempotency_key:
            raise OrderIdempotencyConflictError("Replay idempotency key does not match")
        if expiry != command.reservation_expires_at:
            raise OrderIdempotencyConflictError("Replay reservation expiry does not match")
        pending = await self._pending.internal_get(
            InternalGetPendingActionQuery(
                business_id=command.context.business_id,
                action_id=command.context.pending_action_id,
            )
        )
        payload = pending.payload.get("data")
        if not isinstance(payload, dict):
            raise OrderIdempotencyConflictError("Confirmed payload is unavailable")
        pickup_at = datetime.fromisoformat(str(payload["pickup_at"]))
        persisted_total = sum((line.subtotal for line in lines), Decimal(0))
        if (
            existing.business_id != command.context.business_id
            or existing.pending_action_id != command.context.pending_action_id
            or existing.customer_name != payload.get("customer_name")
            or existing.customer_phone != payload.get("customer_phone")
            or existing.pickup_at != pickup_at
            or existing.total_amount != persisted_total
        ):
            raise OrderIdempotencyConflictError(
                "Persisted order does not match confirmed semantics"
            )
        return lines, expiry

    def _assert_confirmed_command(
        self, command: ConfirmPendingOrderCommand, pending: PendingActionResult
    ) -> None:
        payload_data = pending.payload.get("data")
        if not isinstance(payload_data, dict):
            raise OrderIdempotencyConflictError("Confirmed order payload is invalid")
        confirmed_lines = tuple(
            (int(line["product_id"]), Decimal(str(line["quantity"])))
            for line in payload_data["lines"]
        )
        requested_lines = tuple((line.product_id, line.quantity) for line in command.lines)
        if confirmed_lines != requested_lines:
            raise OrderIdempotencyConflictError("Order lines do not match confirmed snapshot")

    async def _release_reservations(
        self,
        reservations: Sequence[InventoryReservation],
        now: datetime,
        movement_type: InventoryMovementType,
    ) -> None:
        balances = await self._lock_reservation_balances(reservations)
        grouped = self._group_reservations(reservations)
        for key in sorted(grouped):
            current_balance = balances[key]
            for reservation in grouped[key]:
                transition = release_reservation(
                    InventoryState(current_balance.on_hand_qty, current_balance.reserved_qty),
                    reservation.qty,
                    movement_type,
                )
                updated = await self._inventory.update_balance(
                    balance_id=current_balance.id,
                    business_id=reservation.business_id,
                    expected_version=current_balance.version,
                    on_hand_qty=transition.after.on_hand,
                    reserved_qty=transition.after.reserved,
                )
                if updated is None:
                    raise InventoryStaleVersionError("Inventory balance changed concurrently")
                current_balance = updated
                terminal = await self._orders.mark_reservation_terminal(
                    business_id=reservation.business_id,
                    reservation_id=reservation.id,
                    expected_status=InventoryReservationStatus.ACTIVE.value,
                    status=(
                        InventoryReservationStatus.EXPIRED.value
                        if movement_type is InventoryMovementType.RESERVATION_RELEASED
                        else InventoryReservationStatus.RELEASED.value
                    ),
                    released_at=now,
                )
                if terminal is None:
                    raise InventoryStaleVersionError("Reservation terminalized concurrently")
                await self._insert_reservation_movement(reservation, transition)
            balances[key] = current_balance

    async def _consume_reservations(
        self, reservations: Sequence[InventoryReservation], now: datetime
    ) -> None:
        if not reservations:
            raise OrderNotFoundError("Order has no active reservation")
        balances = await self._lock_reservation_balances(reservations)
        grouped = self._group_reservations(reservations)
        for key in sorted(grouped):
            current_balance = balances[key]
            for reservation in grouped[key]:
                transition = complete_pickup(
                    InventoryState(current_balance.on_hand_qty, current_balance.reserved_qty),
                    reservation.qty,
                )
                updated = await self._inventory.update_balance(
                    balance_id=current_balance.id,
                    business_id=reservation.business_id,
                    expected_version=current_balance.version,
                    on_hand_qty=transition.after.on_hand,
                    reserved_qty=transition.after.reserved,
                )
                if updated is None:
                    raise InventoryStaleVersionError("Inventory balance changed concurrently")
                current_balance = updated
                terminal = await self._orders.mark_reservation_terminal(
                    business_id=reservation.business_id,
                    reservation_id=reservation.id,
                    expected_status=InventoryReservationStatus.ACTIVE.value,
                    status=InventoryReservationStatus.COMMITTED.value,
                    released_at=now,
                )
                if terminal is None:
                    raise InventoryStaleVersionError("Reservation terminalized concurrently")
                await self._insert_reservation_movement(reservation, transition)
            balances[key] = current_balance

    async def _lock_reservation_balances(
        self, reservations: Sequence[InventoryReservation]
    ) -> dict[tuple[int, date, int], InventoryBalance]:
        grouped = self._group_reservations(reservations)
        balances: dict[tuple[int, date, int], InventoryBalance] = {}
        for business_id, business_date, product_id in sorted(grouped):
            locked = await self._inventory.lock_balances(business_id, [product_id], business_date)
            if len(locked) != 1:
                raise InventoryBalanceNotFoundError("Required reservation balance is unavailable")
            balances[(business_id, business_date, product_id)] = locked[0]
        return balances

    @staticmethod
    def _group_reservations(
        reservations: Sequence[InventoryReservation],
    ) -> dict[tuple[int, date, int], list[InventoryReservation]]:
        grouped: dict[tuple[int, date, int], list[InventoryReservation]] = {}
        for reservation in reservations:
            key = (
                reservation.business_id,
                reservation.business_date,
                reservation.product_id,
            )
            grouped.setdefault(key, []).append(reservation)
        for rows in grouped.values():
            rows.sort(key=lambda row: row.id)
        return grouped

    async def _insert_reservation_movement(
        self,
        reservation: InventoryReservation,
        transition: InventoryTransition,
    ) -> None:
        await self._inventory.insert_movement(
            {
                "business_id": reservation.business_id,
                "product_id": reservation.product_id,
                "business_date": reservation.business_date,
                "movement_type": transition.movement_type.value,
                "on_hand_delta": transition.on_hand_delta,
                "reserved_delta": transition.reserved_delta,
                "on_hand_after": transition.after.on_hand,
                "reserved_after": transition.after.reserved,
                "available_after": transition.after.available,
                "order_id": reservation.order_id,
                "reservation_id": reservation.id,
                "pending_action_id": reservation.pending_action_id,
                "initiated_by": "order_engine",
            }
        )

    async def _fail_commit(
        self,
        context: CommitResultContext,
        error_code: Literal[
            "temporary_conflict",
            "insufficient_stock",
            "invalid_product",
            "resource_unavailable",
            "transaction_failed",
        ],
        *,
        retryable: bool,
    ) -> None:
        await self._pending.fail_commit(
            FailCommitCommand(
                context=context,
                error_code=error_code,
                retryable=retryable,
            )
        )

    @staticmethod
    def _one_reservation_expiry(
        reservations: Sequence[InventoryReservation],
    ) -> datetime:
        if not reservations:
            raise OrderNotFoundError("Order has no active reservation")
        return OrderService._one_expiry(tuple(row.expires_at for row in reservations))

    @staticmethod
    def _one_expiry(expiries: Sequence[datetime]) -> datetime:
        unique = set(expiries)
        if len(unique) != 1:
            raise OrderIdempotencyConflictError(
                "All reservations for one order must share one expiry"
            )
        return next(iter(unique))

    async def _require_order(self, business_id: int, order_id: int) -> Order:
        order = await self._orders.get_by_id(business_id, order_id)
        if order is None:
            raise OrderNotFoundError("Order was not found")
        return order

    async def _require_locked_order(self, business_id: int, order_id: int) -> Order:
        order = await self._orders.lock_by_id(business_id, order_id)
        if order is None:
            raise OrderNotFoundError("Order was not found")
        return order

    async def _require_timezone(self, business_id: int) -> str:
        timezone = await self._inventory.get_business_timezone(business_id)
        if timezone is None:
            raise OrderNotFoundError("Business was not found")
        return timezone

    async def _authorize_order_read(self, order: Order, actor: ActorContext) -> None:
        if actor.verified_role in {CallerRole.OWNER, CallerRole.MANAGER}:
            await require_owner_or_manager(self._session, actor)
        elif order.customer_phone != actor.normalized_phone:
            raise OrderUnauthorizedError("Customer cannot access another order")

    async def _authorize_order_actor(self, actor: ActorContext, payload: dict[str, object]) -> None:
        if actor.business_id <= 0:
            raise OrderUnauthorizedError("Invalid actor business")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise OrderUnauthorizedError("Confirmed order payload is invalid")
        if actor.verified_role in {CallerRole.OWNER, CallerRole.MANAGER}:
            await require_owner_or_manager(self._session, actor)
        elif data.get("customer_phone") != actor.normalized_phone:
            raise OrderUnauthorizedError("Customer cannot commit another order")

    async def _complete_result(self, order: Order, replay: bool = False) -> OrderResult:
        lines = await self._orders.get_lines(order.business_id, order.id)
        expiries = await self._orders.get_reservation_expiries(order.business_id, order.id)
        expiry = self._one_expiry(expiries) if expiries else None
        return self._to_result(order, lines, expiry, replay)

    async def _authorize_cancellation(
        self, order: Order, phone: str, role: CallerRole, actor: ActorContext
    ) -> None:
        if role in {CallerRole.OWNER, CallerRole.MANAGER}:
            await require_owner_or_manager(self._session, actor)
        elif order.customer_phone != phone:
            raise OrderUnauthorizedError("Customer cannot cancel another order")

    @staticmethod
    def _to_result(
        order: Order,
        lines: Sequence[OrderLineItem],
        reservation_expires_at: datetime | None = None,
        replay: bool = False,
    ) -> OrderResult:
        return OrderResult(
            id=order.id,
            business_id=order.business_id,
            status=OrderStatus(order.status),
            customer_name=order.customer_name,
            customer_phone=order.customer_phone,
            total_amount=order.total_amount,
            pickup_at=order.pickup_at,
            reservation_expires_at=reservation_expires_at,
            lines=tuple(
                OrderLineResult(
                    id=line.id,
                    product_id=line.product_id,
                    product_name=line.product_name_snapshot,
                    quantity=line.qty,
                    unit=ProductUnit(line.unit),
                    price_per_unit=line.price_per_unit_snapshot,
                    subtotal=line.subtotal,
                )
                for line in lines
            ),
            idempotent_replay=replay,
        )
