"""Stateful order-service transaction and lifecycle regressions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from tests.unit.phase_c_fakes import (
    FakeInventoryRepository,
    FakeOrderRepository,
    FakePendingActionService,
    FakePhaseCState,
    FakeSession,
    InjectedFailureError,
)

from fonely.domain.inventory.errors import (
    InsufficientAvailableStockError,
    InvalidProductError,
    InventoryBalanceNotFoundError,
    InventoryStaleVersionError,
)
from fonely.domain.orders.commands import (
    CancelOrderCommand,
    CompletePickupCommand,
    ConfirmOrderLine,
    ConfirmPendingOrderCommand,
    ExpireOrderReservationsCommand,
)
from fonely.domain.orders.errors import OrderIdempotencyConflictError
from fonely.domain.pending_actions.commands import ActorContext, CommitResultContext
from fonely.domain.pending_actions.results import PendingActionResult
from fonely.models.enums import (
    CallerRole,
    InventoryReservationStatus,
    OrderStatus,
    PendingActionStatus,
    PendingActionType,
    ProductUnit,
)
from fonely.services.orders import OrderService

NOW = datetime(2026, 8, 1, 6, tzinfo=UTC)
EXPIRY = NOW + timedelta(hours=2)
PICKUP = NOW + timedelta(hours=1)
BUSINESS_DATE = NOW.astimezone(ZoneInfo("Asia/Kolkata")).date()


def actor(role: CallerRole = CallerRole.CUSTOMER, business_id: int = 1) -> ActorContext:
    phone = "+919222222222" if role is CallerRole.CUSTOMER else "+919123456789"
    return ActorContext(
        business_id=business_id,
        normalized_phone=phone,
        verified_role=role,
    )


def context(version: int = 3) -> CommitResultContext:
    return CommitResultContext(
        business_id=1,
        pending_action_id=10,
        expected_version=version,
        engine="order_engine",
    )


def pending_order(
    *,
    status: PendingActionStatus = PendingActionStatus.AWAITING_CONFIRMATION,
    version: int = 3,
    lines: tuple[tuple[int, str], ...] = ((1, "2"),),
    committed_entity_id: int | None = None,
) -> PendingActionResult:
    return PendingActionResult(
        id=10,
        business_id=1,
        action_type=PendingActionType.ORDER,
        status=status,
        payload_schema_version=1,
        payload={
            "schema_version": 1,
            "action_type": "order",
            "data": {
                "customer_name": "Customer",
                "customer_phone": "+919222222222",
                "pickup_at": PICKUP.isoformat(),
                "lines": [
                    {"product_id": product_id, "quantity": quantity}
                    for product_id, quantity in lines
                ],
            },
        },
        payload_digest="digest",
        confirmation_snapshot="confirmed",
        expires_at=NOW + timedelta(hours=12),
        version=version,
        committed_entity_type="order" if committed_entity_id is not None else None,
        committed_entity_id=committed_entity_id,
        error_code=None,
        rejection_reason_code=None,
        created_at=NOW,
        updated_at=NOW,
        confirmed_at=NOW if status is PendingActionStatus.CONFIRMED else None,
    )


def product(product_id: int, price: str, *, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=product_id,
        business_id=1,
        name=f"Product {product_id}",
        unit=ProductUnit.KG.value,
        price_per_unit=Decimal(price),
        is_active=active,
    )


def balance(product_id: int, on_hand: str = "10") -> SimpleNamespace:
    return SimpleNamespace(
        id=product_id,
        business_id=1,
        product_id=product_id,
        business_date=BUSINESS_DATE,
        on_hand_qty=Decimal(on_hand),
        reserved_qty=Decimal(0),
        available_qty=Decimal(on_hand),
        version=1,
    )


def command(
    *,
    lines: tuple[ConfirmOrderLine, ...] = (ConfirmOrderLine(product_id=1, quantity="2"),),
    expires_at: datetime = EXPIRY,
    key: str = "order-key",
) -> ConfirmPendingOrderCommand:
    return ConfirmPendingOrderCommand(
        context=context(),
        actor=actor(),
        lines=lines,
        now=NOW,
        reservation_expires_at=expires_at,
        idempotency_key=key,
    )


def service_for(state: FakePhaseCState) -> OrderService:
    service = OrderService.__new__(OrderService)
    service._session = FakeSession(state)  # type: ignore[assignment]
    service._inventory = FakeInventoryRepository(state)  # type: ignore[assignment]
    service._orders = FakeOrderRepository(state)  # type: ignore[assignment]
    service._pending = FakePendingActionService(state)  # type: ignore[assignment]
    return service


def confirmation_state(*, multi: bool = False) -> FakePhaseCState:
    state = FakePhaseCState()
    state.products[(1, 1)] = product(1, "100")
    state.balances[(1, BUSINESS_DATE, 1)] = balance(1)
    lines = ((1, "2"),)
    if multi:
        state.products[(1, 2)] = product(2, "50")
        state.balances[(1, BUSINESS_DATE, 2)] = balance(2, "5")
        lines = ((1, "2"), (2, "1"))
    state.pending_actions[10] = pending_order(lines=lines)
    return state


def domain_snapshot(state: FakePhaseCState) -> dict[str, object]:
    snapshot = state.snapshot()
    snapshot.pop("pending_actions")
    return snapshot


async def confirm(state: FakePhaseCState, *, multi: bool = False):
    lines = (ConfirmOrderLine(product_id=1, quantity="2"),)
    if multi:
        lines += (ConfirmOrderLine(product_id=2, quantity="1"),)
    return await service_for(state).confirm(command(lines=lines))


async def test_single_line_confirmation_is_atomic_and_authoritative() -> None:
    state = confirmation_state()
    result = await confirm(state)

    assert result.total_amount == Decimal("200.00")
    assert result.lines[0].product_name == "Product 1"
    assert result.lines[0].price_per_unit == Decimal("100")
    assert result.reservation_expires_at == EXPIRY
    assert state.balances[(1, BUSINESS_DATE, 1)].reserved_qty == Decimal("2")
    assert len(state.orders) == len(state.lines) == len(state.reservations) == 1
    assert len(state.movements) == 1
    assert state.pending_actions[10].status is PendingActionStatus.CONFIRMED
    assert state.events[-2:] == ["pending:complete", "savepoint:release"]


async def test_multi_line_confirmation_uses_sorted_authoritative_snapshots() -> None:
    state = confirmation_state(multi=True)
    result = await confirm(state, multi=True)

    assert [line.product_id for line in result.lines] == [1, 2]
    assert result.total_amount == Decimal("250.00")
    assert {row.expires_at for row in state.reservations.values()} == {EXPIRY}
    assert [state.movements[key].product_id for key in sorted(state.movements)] == [1, 2]


async def test_one_insufficient_line_rolls_back_every_domain_write() -> None:
    state = confirmation_state(multi=True)
    state.balances[(1, BUSINESS_DATE, 2)].on_hand_qty = Decimal(0)
    before = domain_snapshot(state)

    with pytest.raises(InsufficientAvailableStockError):
        await confirm(state, multi=True)

    assert domain_snapshot(state) == before
    assert state.pending_actions[10].status is PendingActionStatus.AWAITING_CONFIRMATION
    assert state.pending_actions[10].error_code == "insufficient_stock"


async def test_inactive_product_fails_and_records_rejection() -> None:
    state = confirmation_state()
    state.products[(1, 1)].is_active = False
    before = domain_snapshot(state)

    with pytest.raises(InvalidProductError):
        await confirm(state)

    assert domain_snapshot(state) == before
    assert state.pending_actions[10].status is PendingActionStatus.REJECTED


@pytest.mark.parametrize(
    "stage",
    [
        "order:insert",
        "lines:insert",
        "reservations:insert",
        "balance:update",
        "movement:insert",
        "pending:complete",
    ],
)
async def test_confirmation_failure_restores_exact_pre_state(stage: str) -> None:
    state = confirmation_state()
    before = domain_snapshot(state)
    state.fail_at = stage

    with pytest.raises(InjectedFailureError):
        await confirm(state)

    assert domain_snapshot(state) == before
    assert state.pending_actions[10].status is PendingActionStatus.REJECTED
    assert state.pending_actions[10].error_code == "transaction_failed"
    assert state.events[-2:] == ["savepoint:rollback", "pending:fail:transaction_failed"]


async def test_balance_stale_version_rolls_back_and_is_retryable() -> None:
    state = confirmation_state()
    repository = FakeInventoryRepository(state)

    async def stale(**values: object) -> None:
        state.events.append(f"stale:{values['balance_id']}")
        return None

    service = service_for(state)
    service._inventory = repository  # type: ignore[assignment]
    repository.update_balance = stale  # type: ignore[method-assign, assignment]
    before = domain_snapshot(state)

    with pytest.raises(InventoryStaleVersionError):
        await service.confirm(command())

    assert domain_snapshot(state) == before
    assert state.pending_actions[10].status is PendingActionStatus.AWAITING_CONFIRMATION
    assert state.pending_actions[10].error_code == "temporary_conflict"


async def test_equivalent_confirmed_replay_returns_complete_aggregate() -> None:
    state = confirmation_state()
    first = await confirm(state)
    replay = await service_for(state).confirm(command())

    assert replay.idempotent_replay
    assert first.model_dump(exclude={"idempotent_replay"}) == replay.model_dump(
        exclude={"idempotent_replay"}
    )
    assert len(state.orders) == len(state.movements) == 1


async def test_committing_replay_completion_failure_records_rejection() -> None:
    state = confirmation_state()
    await confirm(state)
    state.pending_actions[10] = state.pending_actions[10].model_copy(
        update={
            "status": PendingActionStatus.COMMITTING,
            "version": 4,
            "committed_entity_type": None,
            "committed_entity_id": None,
        }
    )
    state.fail_at = "pending:complete"

    with pytest.raises(InjectedFailureError):
        await service_for(state).confirm(command())

    assert state.pending_actions[10].status is PendingActionStatus.REJECTED
    assert state.pending_actions[10].error_code == "transaction_failed"


async def test_committing_replay_repairs_pending_lifecycle() -> None:
    state = confirmation_state()
    first = await confirm(state)
    state.pending_actions[10] = state.pending_actions[10].model_copy(
        update={
            "status": PendingActionStatus.COMMITTING,
            "version": 4,
            "committed_entity_type": None,
            "committed_entity_id": None,
        }
    )
    replay = await service_for(state).confirm(command())

    assert replay.id == first.id
    assert state.pending_actions[10].status is PendingActionStatus.CONFIRMED
    assert state.pending_actions[10].committed_entity_id == first.id


@pytest.mark.parametrize(
    "status",
    [
        PendingActionStatus.AWAITING_CONFIRMATION,
        PendingActionStatus.REJECTED,
        PendingActionStatus.CANCELLED,
        PendingActionStatus.EXPIRED,
    ],
)
async def test_replay_rejects_non_committing_non_confirmed_state(
    status: PendingActionStatus,
) -> None:
    state = confirmation_state()
    await confirm(state)
    state.pending_actions[10] = state.pending_actions[10].model_copy(
        update={"status": status, "version": 5}
    )

    with pytest.raises(OrderIdempotencyConflictError, match="PendingAction state"):
        await service_for(state).confirm(command())


async def test_replay_rejects_different_entity_and_changed_semantics() -> None:
    state = confirmation_state()
    await confirm(state)
    state.pending_actions[10] = state.pending_actions[10].model_copy(
        update={"committed_entity_id": 999}
    )
    with pytest.raises(OrderIdempotencyConflictError, match="different evidence"):
        await service_for(state).confirm(command())

    state.pending_actions[10] = state.pending_actions[10].model_copy(
        update={"committed_entity_id": 1}
    )
    with pytest.raises(OrderIdempotencyConflictError, match="expiry"):
        await service_for(state).confirm(command(expires_at=EXPIRY + timedelta(hours=1)))


async def confirmed_state(*, multi: bool = False):
    state = confirmation_state(multi=multi)
    result = await confirm(state, multi=multi)
    state.events.clear()
    return state, result


@pytest.mark.parametrize(
    "stage", ["balance:update", "reservation:terminal", "movement:insert", "order:status"]
)
async def test_cancellation_failure_restores_all_state(stage: str) -> None:
    state, result = await confirmed_state()
    before = state.snapshot()
    state.fail_at = stage

    with pytest.raises(InjectedFailureError):
        await service_for(state).cancel(
            CancelOrderCommand(actor=actor(), order_id=result.id, now=NOW, idempotency_key="cancel")
        )

    assert state.snapshot() == before
    assert state.events[-1] == "savepoint:rollback"


async def test_cancellation_success_and_replay_return_equivalent_results() -> None:
    state, confirmed = await confirmed_state(multi=True)
    service = service_for(state)
    first = await service.cancel(
        CancelOrderCommand(actor=actor(), order_id=confirmed.id, now=NOW, idempotency_key="cancel")
    )
    replay = await service.cancel(
        CancelOrderCommand(
            actor=actor(), order_id=confirmed.id, now=NOW, idempotency_key="cancel-replay"
        )
    )

    assert first.status is OrderStatus.CANCELLED
    assert first.reservation_expires_at == EXPIRY
    assert first.model_dump(exclude={"idempotent_replay"}) == replay.model_dump(
        exclude={"idempotent_replay"}
    )
    assert len(state.movements) == 4


@pytest.mark.parametrize(
    "stage", ["balance:update", "reservation:terminal", "movement:insert", "order:status"]
)
async def test_pickup_failure_restores_all_state(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, result = await confirmed_state()
    before = state.snapshot()
    state.fail_at = stage
    monkeypatch.setattr(
        "fonely.services.orders.require_owner_or_manager", AsyncMock(return_value=object())
    )

    with pytest.raises(InjectedFailureError):
        await service_for(state).complete_pickup(
            CompletePickupCommand(
                actor=actor(CallerRole.OWNER),
                order_id=result.id,
                now=NOW + timedelta(minutes=30),
                idempotency_key="pickup",
            )
        )

    assert state.snapshot() == before
    assert state.events[-1] == "savepoint:rollback"


async def test_pickup_success_and_replay_return_equivalent_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, confirmed = await confirmed_state()
    monkeypatch.setattr(
        "fonely.services.orders.require_owner_or_manager", AsyncMock(return_value=object())
    )
    service = service_for(state)
    first = await service.complete_pickup(
        CompletePickupCommand(
            actor=actor(CallerRole.OWNER),
            order_id=confirmed.id,
            now=NOW + timedelta(minutes=30),
            idempotency_key="pickup",
        )
    )
    replay = await service.complete_pickup(
        CompletePickupCommand(
            actor=actor(CallerRole.OWNER),
            order_id=confirmed.id,
            now=NOW + timedelta(minutes=31),
            idempotency_key="pickup-replay",
        )
    )

    assert first.status is OrderStatus.PICKED_UP
    assert first.model_dump(exclude={"idempotent_replay"}) == replay.model_dump(
        exclude={"idempotent_replay"}
    )
    assert state.balances[(1, BUSINESS_DATE, 1)].on_hand_qty == Decimal("8")
    assert state.balances[(1, BUSINESS_DATE, 1)].reserved_qty == Decimal(0)


async def test_expiry_releases_complete_order_at_exact_boundary() -> None:
    state, confirmed = await confirmed_state(multi=True)
    result = await service_for(state).expire(
        ExpireOrderReservationsCommand(now=EXPIRY, batch_size=10)
    )

    assert result.expired_order_ids == (confirmed.id,)
    assert result.count == 2
    assert state.orders[confirmed.id].status == OrderStatus.CANCELLED.value
    assert all(
        row.status == InventoryReservationStatus.EXPIRED.value
        for row in state.reservations.values()
    )
    assert await FakeInventoryRepository(state).count_active_reservations(1, confirmed.id) == 0
    repeated = await service_for(state).expire(
        ExpireOrderReservationsCommand(now=EXPIRY, batch_size=10)
    )
    assert repeated.count == 0


async def test_expiry_rejects_mixed_expiries_and_rolls_back() -> None:
    state, confirmed = await confirmed_state(multi=True)
    state.reservations[2].expires_at = EXPIRY + timedelta(hours=1)
    before = state.snapshot()

    with pytest.raises(OrderIdempotencyConflictError, match="one expiry"):
        await service_for(state).expire(ExpireOrderReservationsCommand(now=EXPIRY, batch_size=10))

    assert state.snapshot() == before
    assert state.orders[confirmed.id].status == OrderStatus.CONFIRMED.value


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("missing", InventoryBalanceNotFoundError),
        ("reservation:terminal", InjectedFailureError),
        ("movement:insert", InjectedFailureError),
        ("order:status", InjectedFailureError),
    ],
)
async def test_expiry_failures_restore_order(failure: str, expected: type[Exception]) -> None:
    state, confirmed = await confirmed_state()
    before = state.snapshot()
    if failure == "missing":
        state.balances.clear()
    else:
        state.fail_at = failure
    with pytest.raises(expected):
        await service_for(state).expire(ExpireOrderReservationsCommand(now=EXPIRY, batch_size=10))
    if failure != "missing":
        assert state.snapshot() == before
    assert state.orders[confirmed.id].status == OrderStatus.CONFIRMED.value


def test_reversed_reservations_produce_identical_lock_order() -> None:
    rows = [
        SimpleNamespace(id=index, business_id=1, business_date=business_date, product_id=pid)
        for index, (business_date, pid) in enumerate(
            [
                (BUSINESS_DATE + timedelta(days=1), 2),
                (BUSINESS_DATE, 2),
                (BUSINESS_DATE, 1),
                (BUSINESS_DATE, 1),
            ],
            start=1,
        )
    ]
    first = OrderService._group_reservations(rows)
    second = OrderService._group_reservations(tuple(reversed(rows)))
    assert (
        sorted(first)
        == sorted(second)
        == [
            (1, BUSINESS_DATE, 1),
            (1, BUSINESS_DATE, 2),
            (1, BUSINESS_DATE + timedelta(days=1), 2),
        ]
    )
    assert [row.id for row in first[(1, BUSINESS_DATE, 1)]] == [3, 4]
