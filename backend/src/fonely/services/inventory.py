"""Deterministic inventory transactions within a caller-owned session transaction."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.exceptions import FonelyError
from fonely.domain.inventory.calculations import InventoryState, add_stock, sell_walk_in, set_stock
from fonely.domain.inventory.commands import (
    AddOwnerStockCommand,
    CommitOwnerStockCommand,
    GetInventoryAvailabilityQuery,
    RecordWalkInSaleCommand,
    SetOwnerStockCommand,
    VerifyLedgerConsistencyQuery,
)
from fonely.domain.inventory.errors import (
    InvalidProductError,
    InventoryBalanceNotFoundError,
    InventoryIdempotencyConflictError,
    InventoryStaleVersionError,
    InventoryTenantMismatchError,
    ReservedStockViolationError,
)
from fonely.domain.inventory.policies import derive_business_date
from fonely.domain.inventory.results import (
    InventoryAvailabilityResult,
    InventoryMutationResult,
    LedgerConsistencyResult,
    LedgerDiscrepancy,
)
from fonely.domain.pending_actions.commands import (
    BeginCommitCommand,
    CompleteCommitCommand,
    FailCommitCommand,
    InternalGetPendingActionQuery,
)
from fonely.domain.pending_actions.payloads import OwnerStockUpdateEnvelope, validate_payload
from fonely.models.enums import (
    InventoryMovementType,
    PendingActionStatus,
    PendingActionType,
    ProductUnit,
)
from fonely.models.schema import InventoryMovement
from fonely.repositories.inventory import InventoryRepository
from fonely.services.authorization import require_owner_or_manager
from fonely.services.pending_actions import PendingActionService


class InventoryService:
    """Coordinates inventory policy without committing or rolling back the session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = InventoryRepository(session)
        self._pending = PendingActionService(session)

    async def availability(
        self, query: GetInventoryAvailabilityQuery
    ) -> tuple[InventoryAvailabilityResult, ...]:
        timezone = await self._require_timezone(query.business_id)
        business_date = derive_business_date(query.at, timezone)
        products = await self._repo.get_active_products(query.business_id, query.product_ids)
        if query.product_ids and len(products) != len(set(query.product_ids)):
            raise InvalidProductError("One or more products are unavailable")
        balances = {
            balance.product_id: balance
            for balance in await self._repo.get_balances(
                query.business_id,
                [product.id for product in products],
                business_date,
            )
        }
        return tuple(
            InventoryAvailabilityResult(
                business_id=query.business_id,
                product_id=product.id,
                product_name=product.name,
                unit=ProductUnit(product.unit),
                business_date=business_date,
                on_hand_qty=(
                    balance.on_hand_qty if (balance := balances.get(product.id)) else Decimal(0)
                ),
                reserved_qty=balance.reserved_qty if balance else Decimal(0),
                available_qty=balance.available_qty if balance else Decimal(0),
            )
            for product in products
        )

    async def set_stock(self, command: SetOwnerStockCommand) -> InventoryMutationResult:
        await require_owner_or_manager(self._session, command.actor)
        return await self._mutate(
            business_id=command.actor.business_id,
            product_id=command.product_id,
            quantity=command.quantity,
            occurred_at=command.occurred_at,
            operation="set",
            note=command.note,
            initiated_by=command.actor.normalized_phone,
        )

    async def add_stock(self, command: AddOwnerStockCommand) -> InventoryMutationResult:
        await require_owner_or_manager(self._session, command.actor)
        return await self._mutate(
            business_id=command.actor.business_id,
            product_id=command.product_id,
            quantity=command.quantity,
            occurred_at=command.occurred_at,
            operation="add",
            note=command.note,
            initiated_by=command.actor.normalized_phone,
        )

    async def record_walk_in(self, command: RecordWalkInSaleCommand) -> InventoryMutationResult:
        await require_owner_or_manager(self._session, command.actor)
        return await self._mutate(
            business_id=command.actor.business_id,
            product_id=command.product_id,
            quantity=command.quantity,
            occurred_at=command.occurred_at,
            operation="walk_in",
            note=command.note,
            initiated_by=command.actor.normalized_phone,
        )

    async def commit_owner_stock(self, command: CommitOwnerStockCommand) -> InventoryMutationResult:
        await require_owner_or_manager(self._session, command.actor)

        if command.actor.business_id != command.context.business_id:
            raise InventoryTenantMismatchError("Actor business does not match commit context")

        pending = await self._pending.internal_get(
            InternalGetPendingActionQuery(
                business_id=command.context.business_id,
                action_id=command.context.pending_action_id,
            )
        )

        if pending.action_type != PendingActionType.OWNER_STOCK_UPDATE:
            raise InventoryTenantMismatchError("PendingAction is not an owner stock update")

        if pending.confirmation_snapshot is None:
            raise InventoryStaleVersionError("PendingAction has no confirmed snapshot")

        envelope = validate_payload(
            PendingActionType.OWNER_STOCK_UPDATE,
            pending.payload_schema_version,
            pending.payload,
        )
        if not isinstance(envelope, OwnerStockUpdateEnvelope):
            raise InventoryTenantMismatchError("Payload type mismatch")

        payload = envelope.data
        timezone = await self._require_timezone(command.context.business_id)
        derived_date = derive_business_date(command.occurred_at, timezone)

        if str(derived_date) != str(payload.business_date):
            raise InventoryIdempotencyConflictError(
                "Derived business date does not match confirmed payload"
            )
        if command.operation != payload.operation:
            raise InventoryIdempotencyConflictError("Operation does not match confirmed payload")
        if command.quantity != payload.quantity:
            raise InventoryIdempotencyConflictError("Quantity does not match confirmed payload")

        existing_movement = await self._repo.get_movement_for_pending_action(
            command.context.business_id, command.context.pending_action_id
        )
        if existing_movement is not None:
            result = self._movement_replay_result(
                existing_movement,
                product_id=payload.product_id,
                operation=command.operation,
                quantity=command.quantity,
                business_date=derived_date,
            )
            if pending.status is PendingActionStatus.CONFIRMED:
                if (
                    pending.committed_entity_type != "inventory_update"
                    or pending.committed_entity_id != existing_movement.id
                ):
                    raise InventoryIdempotencyConflictError(
                        "PendingAction is confirmed with different movement evidence"
                    )
                return result
            if pending.status is PendingActionStatus.COMMITTING:
                repair_context = command.context.model_copy(
                    update={"expected_version": pending.version}
                )
                try:
                    async with self._session.begin_nested():
                        await self._pending.complete_commit(
                            CompleteCommitCommand(
                                context=repair_context,
                                committed_entity_type="inventory_update",
                                committed_entity_id=existing_movement.id,
                            )
                        )
                except (FonelyError, SQLAlchemyError):
                    await self._pending.fail_commit(
                        FailCommitCommand(
                            context=repair_context,
                            error_code="transaction_failed",
                            retryable=False,
                        )
                    )
                    raise
                return result
            raise InventoryIdempotencyConflictError(
                f"Movement evidence conflicts with PendingAction state {pending.status.value}"
            )

        committing = await self._pending.begin_commit(BeginCommitCommand(context=command.context))

        try:
            async with self._session.begin_nested():
                products = await self._repo.lock_active_products(
                    command.context.business_id, [payload.product_id]
                )
                if len(products) != 1:
                    raise InvalidProductError("Product is unavailable")

                await self._repo.ensure_balance(
                    command.context.business_id, payload.product_id, derived_date
                )
                balances = await self._repo.lock_balances(
                    command.context.business_id, [payload.product_id], derived_date
                )
                if len(balances) != 1:
                    raise InventoryBalanceNotFoundError("Inventory balance could not be locked")
                balance = balances[0]
                before = InventoryState(balance.on_hand_qty, balance.reserved_qty)

                if command.operation == "set":
                    transition = set_stock(before, command.quantity)
                else:
                    transition = add_stock(before, command.quantity)

                updated = await self._repo.update_balance(
                    balance_id=balance.id,
                    business_id=command.context.business_id,
                    expected_version=balance.version,
                    on_hand_qty=transition.after.on_hand,
                    reserved_qty=transition.after.reserved,
                )
                if updated is None:
                    raise InventoryStaleVersionError("Inventory balance changed concurrently")

                movement = await self._repo.insert_movement(
                    {
                        "business_id": command.context.business_id,
                        "product_id": payload.product_id,
                        "business_date": derived_date,
                        "movement_type": transition.movement_type.value,
                        "on_hand_delta": transition.on_hand_delta,
                        "reserved_delta": transition.reserved_delta,
                        "on_hand_after": transition.after.on_hand,
                        "reserved_after": transition.after.reserved,
                        "available_after": transition.after.available,
                        "pending_action_id": command.context.pending_action_id,
                        "initiated_by": command.actor.normalized_phone,
                        "note": payload.note,
                    }
                )
                await self._pending.complete_commit(
                    CompleteCommitCommand(
                        context=command.context.model_copy(
                            update={"expected_version": committing.version}
                        ),
                        committed_entity_type="inventory_update",
                        committed_entity_id=movement.id,
                    )
                )

        except ReservedStockViolationError:
            await self._pending.fail_commit(
                FailCommitCommand(
                    context=command.context.model_copy(
                        update={"expected_version": committing.version}
                    ),
                    error_code="insufficient_stock",
                    retryable=True,
                )
            )
            raise
        except InvalidProductError:
            await self._pending.fail_commit(
                FailCommitCommand(
                    context=command.context.model_copy(
                        update={"expected_version": committing.version}
                    ),
                    error_code="invalid_product",
                    retryable=False,
                )
            )
            raise
        except InventoryStaleVersionError:
            await self._pending.fail_commit(
                FailCommitCommand(
                    context=command.context.model_copy(
                        update={"expected_version": committing.version}
                    ),
                    error_code="temporary_conflict",
                    retryable=True,
                )
            )
            raise
        except (FonelyError, SQLAlchemyError):
            await self._pending.fail_commit(
                FailCommitCommand(
                    context=command.context.model_copy(
                        update={"expected_version": committing.version}
                    ),
                    error_code="transaction_failed",
                    retryable=False,
                )
            )
            raise

        return InventoryMutationResult(
            business_id=command.context.business_id,
            product_id=payload.product_id,
            business_date=derived_date,
            movement_id=movement.id,
            movement_type=InventoryMovementType(movement.movement_type),
            on_hand_delta=movement.on_hand_delta,
            reserved_delta=movement.reserved_delta,
            on_hand_after=movement.on_hand_after,
            reserved_after=movement.reserved_after,
            available_after=movement.available_after,
        )

    @staticmethod
    def _movement_replay_result(
        movement: InventoryMovement,
        *,
        product_id: int,
        operation: str,
        quantity: Decimal,
        business_date: object,
    ) -> InventoryMutationResult:
        expected_type = (
            InventoryMovementType.MANUAL_ADJUSTMENT
            if operation == "set"
            else InventoryMovementType.STOCK_ADDED
        )
        movement_type = InventoryMovementType(movement.movement_type)
        quantity_matches = (
            movement.on_hand_after == quantity
            if operation == "set"
            else movement.on_hand_delta == quantity
        )
        if (
            movement.product_id != product_id
            or movement_type is not expected_type
            or movement.business_date != business_date
            or not quantity_matches
            or movement.reserved_delta != 0
        ):
            raise InventoryIdempotencyConflictError("Existing movement conflicts with this commit")
        return InventoryMutationResult(
            business_id=movement.business_id,
            product_id=movement.product_id,
            business_date=movement.business_date,
            movement_id=movement.id,
            movement_type=movement_type,
            on_hand_delta=movement.on_hand_delta,
            reserved_delta=movement.reserved_delta,
            on_hand_after=movement.on_hand_after,
            reserved_after=movement.reserved_after,
            available_after=movement.available_after,
            idempotent_replay=True,
        )

    async def verify_ledger(self, query: VerifyLedgerConsistencyQuery) -> LedgerConsistencyResult:
        await self._require_timezone(query.business_id)
        balances = {
            (balance.product_id, balance.business_date): balance
            for balance in await self._repo.get_all_balances(query.business_id, query.product_id)
        }
        totals = {
            (product_id, business_date): (ledger_on_hand, ledger_reserved)
            for product_id, business_date, ledger_on_hand, ledger_reserved in (
                await self._repo.ledger_totals(query.business_id)
            )
            if query.product_id is None or product_id == query.product_id
        }
        discrepancies: list[LedgerDiscrepancy] = []
        for product_id, business_date in sorted(set(balances) | set(totals)):
            balance = balances.get((product_id, business_date))
            ledger_on_hand, ledger_reserved = totals.get(
                (product_id, business_date), (Decimal(0), Decimal(0))
            )
            balance_on_hand = balance.on_hand_qty if balance else Decimal(0)
            balance_reserved = balance.reserved_qty if balance else Decimal(0)
            if balance_on_hand != ledger_on_hand or balance_reserved != ledger_reserved:
                discrepancies.append(
                    LedgerDiscrepancy(
                        business_id=query.business_id,
                        product_id=product_id,
                        business_date=business_date,
                        balance_on_hand=balance_on_hand,
                        ledger_on_hand=ledger_on_hand,
                        balance_reserved=balance_reserved,
                        ledger_reserved=ledger_reserved,
                    )
                )
        return LedgerConsistencyResult(
            business_id=query.business_id,
            discrepancies=tuple(discrepancies),
        )

    async def _mutate(
        self,
        *,
        business_id: int,
        product_id: int,
        quantity: Decimal,
        occurred_at: datetime,
        operation: str,
        note: str | None,
        initiated_by: str,
    ) -> InventoryMutationResult:
        async with self._session.begin_nested():
            timezone = await self._require_timezone(business_id)
            business_date = derive_business_date(occurred_at, timezone)
            products = await self._repo.lock_active_products(business_id, [product_id])
            if len(products) != 1:
                raise InvalidProductError("Product is unavailable")
            await self._repo.ensure_balance(business_id, product_id, business_date)
            balances = await self._repo.lock_balances(business_id, [product_id], business_date)
            if len(balances) != 1:
                raise InventoryBalanceNotFoundError("Inventory balance could not be locked")
            balance = balances[0]
            before = InventoryState(balance.on_hand_qty, balance.reserved_qty)
            if operation == "set":
                transition = set_stock(before, quantity)
            elif operation == "add":
                transition = add_stock(before, quantity)
            else:
                transition = sell_walk_in(before, quantity)
            updated = await self._repo.update_balance(
                balance_id=balance.id,
                business_id=business_id,
                expected_version=balance.version,
                on_hand_qty=transition.after.on_hand,
                reserved_qty=transition.after.reserved,
            )
            if updated is None:
                raise InventoryStaleVersionError("Inventory balance changed concurrently")
            movement = await self._repo.insert_movement(
                {
                    "business_id": business_id,
                    "product_id": product_id,
                    "business_date": business_date,
                    "movement_type": transition.movement_type.value,
                    "on_hand_delta": transition.on_hand_delta,
                    "reserved_delta": transition.reserved_delta,
                    "on_hand_after": transition.after.on_hand,
                    "reserved_after": transition.after.reserved,
                    "available_after": transition.after.available,
                    "initiated_by": initiated_by,
                    "note": note,
                }
            )
            return InventoryMutationResult(
                business_id=business_id,
                product_id=product_id,
                business_date=business_date,
                movement_id=movement.id,
                movement_type=InventoryMovementType(movement.movement_type),
                on_hand_delta=movement.on_hand_delta,
                reserved_delta=movement.reserved_delta,
                on_hand_after=movement.on_hand_after,
                reserved_after=movement.reserved_after,
                available_after=movement.available_after,
            )

    async def _require_timezone(self, business_id: int) -> str:
        timezone = await self._repo.get_business_timezone(business_id)
        if timezone is None:
            raise InvalidProductError("Business scope does not exist")
        return timezone
