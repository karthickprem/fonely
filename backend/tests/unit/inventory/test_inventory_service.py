"""Stateful inventory-service transaction and idempotency regressions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from tests.unit.phase_c_fakes import (
    FakeInventoryRepository,
    FakePendingActionService,
    FakePhaseCState,
    FakeSession,
    InjectedFailureError,
)

from fonely.domain.inventory.commands import (
    AddOwnerStockCommand,
    CommitOwnerStockCommand,
    RecordWalkInSaleCommand,
    SetOwnerStockCommand,
    VerifyLedgerConsistencyQuery,
)
from fonely.domain.inventory.errors import (
    InsufficientAvailableStockError,
    InventoryIdempotencyConflictError,
    InventoryStaleVersionError,
    ReservedStockViolationError,
)
from fonely.domain.inventory.policies import (
    DirectInventoryRequestSignature,
    direct_inventory_requests_equivalent,
)
from fonely.domain.pending_actions.commands import ActorContext, CommitResultContext
from fonely.domain.pending_actions.errors import PendingActionUnauthorizedError
from fonely.domain.pending_actions.results import PendingActionResult
from fonely.models.enums import (
    CallerRole,
    Channel,
    InventoryMovementType,
    PendingActionStatus,
    PendingActionType,
    ProductUnit,
)
from fonely.services.inventory import InventoryService

NOW = datetime(2026, 8, 1, 6, tzinfo=UTC)
BUSINESS_DATE = NOW.astimezone(ZoneInfo("Asia/Kolkata")).date()


def actor(role: CallerRole = CallerRole.OWNER) -> ActorContext:
    phones = {
        CallerRole.OWNER: "+919123456789",
        CallerRole.MANAGER: "+919111111111",
        CallerRole.CUSTOMER: "+919222222222",
    }
    return ActorContext(
        business_id=1,
        normalized_phone=phones[role],
        verified_role=role,
        channel=Channel.TEXT,
    )


def context(version: int = 3) -> CommitResultContext:
    return CommitResultContext(
        business_id=1,
        pending_action_id=10,
        expected_version=version,
        engine="inventory_engine",
    )


def pending_stock(
    *,
    status: PendingActionStatus = PendingActionStatus.AWAITING_CONFIRMATION,
    operation: str = "set",
    quantity: str = "5",
    committed_entity_id: int | None = None,
) -> PendingActionResult:
    return PendingActionResult(
        id=10,
        business_id=1,
        action_type=PendingActionType.OWNER_STOCK_UPDATE,
        status=status,
        payload_schema_version=1,
        payload={
            "schema_version": 1,
            "action_type": "owner_stock_update",
            "data": {
                "product_id": 1,
                "business_date": BUSINESS_DATE.isoformat(),
                "operation": operation,
                "quantity": quantity,
                "note": "counted",
            },
        },
        payload_digest="digest",
        confirmation_snapshot="confirmed",
        expires_at=NOW + timedelta(hours=12),
        version=3 if status is PendingActionStatus.AWAITING_CONFIRMATION else 4,
        committed_entity_type=("inventory_update" if committed_entity_id is not None else None),
        committed_entity_id=committed_entity_id,
        error_code=None,
        rejection_reason_code=None,
        created_at=NOW,
        updated_at=NOW,
        confirmed_at=NOW if status is PendingActionStatus.CONFIRMED else None,
    )


def state_with_stock(*, on_hand: str = "2", reserved: str = "0") -> FakePhaseCState:
    state = FakePhaseCState()
    state.products[(1, 1)] = SimpleNamespace(
        id=1,
        business_id=1,
        name="Rice",
        unit=ProductUnit.KG.value,
        price_per_unit=Decimal("100"),
        is_active=True,
    )
    state.balances[(1, BUSINESS_DATE, 1)] = SimpleNamespace(
        id=1,
        business_id=1,
        product_id=1,
        business_date=BUSINESS_DATE,
        on_hand_qty=Decimal(on_hand),
        reserved_qty=Decimal(reserved),
        available_qty=Decimal(on_hand) - Decimal(reserved),
        version=1,
    )
    return state


def service_for(state: FakePhaseCState) -> InventoryService:
    service = InventoryService.__new__(InventoryService)
    service._session = FakeSession(state)  # type: ignore[assignment]
    service._repo = FakeInventoryRepository(state)  # type: ignore[assignment]
    service._pending = FakePendingActionService(state)  # type: ignore[assignment]
    return service


def authorization_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fonely.services.inventory.require_owner_or_manager", AsyncMock(return_value=object())
    )


@pytest.mark.parametrize(
    "method,command,expected",
    [
        (
            "set_stock",
            SetOwnerStockCommand(
                actor=actor(),
                product_id=1,
                quantity="0",
                occurred_at=NOW,
                idempotency_key="set-zero",
            ),
            Decimal(0),
        ),
        (
            "add_stock",
            AddOwnerStockCommand(
                actor=actor(),
                product_id=1,
                quantity="3",
                occurred_at=NOW,
                idempotency_key="add",
            ),
            Decimal("5"),
        ),
        (
            "record_walk_in",
            RecordWalkInSaleCommand(
                actor=actor(),
                product_id=1,
                quantity="1",
                occurred_at=NOW,
                idempotency_key="walk-in",
            ),
            Decimal("1"),
        ),
    ],
)
async def test_direct_inventory_mutations_have_balance_and_movement_evidence(
    method: str,
    command: object,
    expected: Decimal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_with_stock()
    authorization_allowed(monkeypatch)

    result = await getattr(service_for(state), method)(command)

    assert result.on_hand_after == expected
    assert state.balances[(1, BUSINESS_DATE, 1)].on_hand_qty == expected
    assert len(state.movements) == 1


async def test_direct_movement_failure_rolls_back_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_with_stock()
    before = state.snapshot()
    state.fail_at = "movement:insert"
    authorization_allowed(monkeypatch)

    with pytest.raises(InjectedFailureError):
        await service_for(state).add_stock(
            AddOwnerStockCommand(
                actor=actor(),
                product_id=1,
                quantity="3",
                occurred_at=NOW,
                idempotency_key="add-failure",
            )
        )
    assert state.snapshot() == before
    assert state.events[-1] == "savepoint:rollback"


async def test_reserved_stock_rejections_leave_state_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_with_stock(on_hand="5", reserved="3")
    authorization_allowed(monkeypatch)
    before = state.snapshot()

    with pytest.raises(ReservedStockViolationError):
        await service_for(state).set_stock(
            SetOwnerStockCommand(
                actor=actor(),
                product_id=1,
                quantity="2",
                occurred_at=NOW,
                idempotency_key="below-reserved",
            )
        )
    with pytest.raises(InsufficientAvailableStockError):
        await service_for(state).record_walk_in(
            RecordWalkInSaleCommand(
                actor=actor(),
                product_id=1,
                quantity="3",
                occurred_at=NOW,
                idempotency_key="consume-reserved",
            )
        )
    assert state.snapshot() == before


def owner_commit_command(
    role: CallerRole = CallerRole.OWNER,
    *,
    operation: str = "set",
    quantity: str = "5",
) -> CommitOwnerStockCommand:
    return CommitOwnerStockCommand(
        context=context(),
        actor=actor(role),
        operation=operation,
        quantity=quantity,
        occurred_at=NOW,
        idempotency_key="owner-stock",
    )


@pytest.mark.parametrize("role", [CallerRole.OWNER, CallerRole.MANAGER])
async def test_owner_stock_commit_succeeds_for_owner_and_manager(
    role: CallerRole, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = state_with_stock()
    state.pending_actions[10] = pending_stock()
    authorization_allowed(monkeypatch)

    result = await service_for(state).commit_owner_stock(owner_commit_command(role))

    assert result.on_hand_after == Decimal("5")
    assert state.pending_actions[10].status is PendingActionStatus.CONFIRMED
    assert state.pending_actions[10].committed_entity_id == result.movement_id
    assert state.movements[result.movement_id].pending_action_id == 10
    assert state.events[-2:] == ["pending:complete", "savepoint:release"]


async def test_customer_is_denied_owner_stock_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fonely.services.inventory.require_owner_or_manager",
        AsyncMock(side_effect=PendingActionUnauthorizedError("denied")),
    )
    state = state_with_stock()
    state.pending_actions[10] = pending_stock()

    with pytest.raises(PendingActionUnauthorizedError):
        await service_for(state).commit_owner_stock(owner_commit_command(CallerRole.CUSTOMER))
    assert not state.movements


@pytest.mark.parametrize("stage", ["movement:insert", "pending:complete"])
async def test_owner_stock_failure_restores_exact_state_and_records_failure(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = state_with_stock()
    state.pending_actions[10] = pending_stock()
    before = state.snapshot()
    state.fail_at = stage
    authorization_allowed(monkeypatch)

    with pytest.raises(InjectedFailureError):
        await service_for(state).commit_owner_stock(owner_commit_command())

    before.pop("pending_actions")
    snapshot = state.snapshot()
    snapshot.pop("pending_actions")
    assert snapshot == before
    assert state.pending_actions[10].status is PendingActionStatus.REJECTED
    assert state.pending_actions[10].error_code == "transaction_failed"


async def test_owner_stock_stale_balance_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_with_stock()
    state.pending_actions[10] = pending_stock()
    authorization_allowed(monkeypatch)
    repository = FakeInventoryRepository(state)

    async def stale(**values: object) -> None:
        state.events.append(f"stale:{values['balance_id']}")
        return None

    repository.update_balance = stale  # type: ignore[method-assign, assignment]
    service = service_for(state)
    service._repo = repository  # type: ignore[assignment]

    with pytest.raises(InventoryStaleVersionError):
        await service.commit_owner_stock(owner_commit_command())
    assert state.pending_actions[10].status is PendingActionStatus.AWAITING_CONFIRMATION
    assert state.pending_actions[10].error_code == "temporary_conflict"


async def test_owner_stock_repair_failure_records_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_with_stock()
    state.pending_actions[10] = pending_stock()
    authorization_allowed(monkeypatch)
    service = service_for(state)
    await service.commit_owner_stock(owner_commit_command())
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
        await service.commit_owner_stock(owner_commit_command())

    assert state.pending_actions[10].status is PendingActionStatus.REJECTED
    assert state.pending_actions[10].error_code == "transaction_failed"


async def test_owner_stock_confirmed_replay_and_committing_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_with_stock()
    state.pending_actions[10] = pending_stock()
    authorization_allowed(monkeypatch)
    service = service_for(state)
    first = await service.commit_owner_stock(owner_commit_command())
    replay = await service.commit_owner_stock(owner_commit_command())
    assert replay.idempotent_replay
    assert len(state.movements) == 1

    state.pending_actions[10] = state.pending_actions[10].model_copy(
        update={
            "status": PendingActionStatus.COMMITTING,
            "version": 4,
            "committed_entity_type": None,
            "committed_entity_id": None,
        }
    )
    repaired = await service.commit_owner_stock(owner_commit_command())
    assert repaired.movement_id == first.movement_id
    assert state.pending_actions[10].status is PendingActionStatus.CONFIRMED


@pytest.mark.parametrize(
    "status",
    [
        PendingActionStatus.AWAITING_CONFIRMATION,
        PendingActionStatus.REJECTED,
        PendingActionStatus.CANCELLED,
        PendingActionStatus.EXPIRED,
    ],
)
async def test_owner_stock_replay_rejects_invalid_lifecycle(
    status: PendingActionStatus, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = state_with_stock()
    state.pending_actions[10] = pending_stock()
    authorization_allowed(monkeypatch)
    service = service_for(state)
    await service.commit_owner_stock(owner_commit_command())
    state.pending_actions[10] = state.pending_actions[10].model_copy(
        update={"status": status, "version": 5}
    )

    with pytest.raises(InventoryIdempotencyConflictError, match="PendingAction state"):
        await service.commit_owner_stock(owner_commit_command())


async def test_owner_stock_replay_rejects_changed_evidence_or_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_with_stock()
    state.pending_actions[10] = pending_stock()
    authorization_allowed(monkeypatch)
    service = service_for(state)
    await service.commit_owner_stock(owner_commit_command())
    state.pending_actions[10] = state.pending_actions[10].model_copy(
        update={"committed_entity_id": 999}
    )
    with pytest.raises(InventoryIdempotencyConflictError, match="different movement"):
        await service.commit_owner_stock(owner_commit_command())

    with pytest.raises(InventoryIdempotencyConflictError, match="Quantity"):
        await service.commit_owner_stock(owner_commit_command(quantity="6"))


async def test_ledger_reports_consistency_and_discrepancy() -> None:
    state = state_with_stock(on_hand="5")
    state.movements[1] = SimpleNamespace(
        id=1,
        business_id=1,
        product_id=1,
        business_date=BUSINESS_DATE,
        movement_type=InventoryMovementType.STOCK_ADDED.value,
        on_hand_delta=Decimal("5"),
        reserved_delta=Decimal(0),
    )
    service = service_for(state)
    consistent = await service.verify_ledger(VerifyLedgerConsistencyQuery(business_id=1))
    assert consistent.consistent
    state.movements[1].on_hand_delta = Decimal("4")
    discrepancy = await service.verify_ledger(VerifyLedgerConsistencyQuery(business_id=1))
    assert not discrepancy.consistent
    assert discrepancy.discrepancies[0].ledger_on_hand == Decimal("4")


def test_direct_inventory_request_signature_is_semantic_and_not_persistent() -> None:
    first = DirectInventoryRequestSignature(
        business_id=1,
        operation="add",
        product_id=1,
        quantity=Decimal("2.00"),
        occurred_at=NOW,
        note="counted",
    )
    equivalent = DirectInventoryRequestSignature(
        business_id=1,
        operation="add",
        product_id=1,
        quantity=Decimal("2"),
        occurred_at=NOW,
        note="counted",
    )
    changed = DirectInventoryRequestSignature(
        business_id=1,
        operation="add",
        product_id=1,
        quantity=Decimal("3"),
        occurred_at=NOW,
        note="counted",
    )
    assert direct_inventory_requests_equivalent(first, equivalent)
    assert first.digest == equivalent.digest
    assert not direct_inventory_requests_equivalent(first, changed)
    assert first.digest != changed.digest


def test_cross_tenant_owner_commit_is_rejected_by_command() -> None:
    with pytest.raises(ValidationError, match="same business"):
        CommitOwnerStockCommand(
            context=context(),
            actor=ActorContext(
                business_id=2,
                normalized_phone="+919123456789",
                verified_role=CallerRole.OWNER,
                channel=Channel.TEXT,
            ),
            operation="set",
            quantity="5",
            occurred_at=NOW,
            idempotency_key="wrong-tenant",
        )


# =============================================================================
# _is_unique_violation classifier
# =============================================================================


class TestIsUniqueViolation:
    def _make_exc(
        self,
        *,
        sqlstate: str | None = "23505",
        constraint_name: str | None = "uq_inv_op_idempotency",
        use_cause_chain: bool = False,
    ) -> IntegrityError:
        from types import SimpleNamespace

        from sqlalchemy.exc import IntegrityError as SA_IntegrityError

        if use_cause_chain:
            asyncpg_inner = SimpleNamespace(
                sqlstate=sqlstate,
                constraint_name=constraint_name,
            )
            driver = SimpleNamespace(
                sqlstate=sqlstate,
                constraint_name=None,
                __cause__=asyncpg_inner,
            )
        else:
            driver = SimpleNamespace(
                sqlstate=sqlstate,
                constraint_name=constraint_name,
                __cause__=None,
            )
        exc = SA_IntegrityError("test", {}, Exception("inner"))
        exc.orig = driver  # type: ignore[assignment]
        return exc

    def test_exact_sqlstate_and_constraint_recovers(self) -> None:
        from fonely.services.inventory import _is_unique_violation

        exc = self._make_exc()
        assert _is_unique_violation(exc, "uq_inv_op_idempotency") is True

    def test_same_sqlstate_wrong_constraint_reraises(self) -> None:
        from fonely.services.inventory import _is_unique_violation

        exc = self._make_exc(constraint_name="uq_order_idempotency")
        assert _is_unique_violation(exc, "uq_inv_op_idempotency") is False

    def test_constraint_text_only_in_message_reraises(self) -> None:
        from types import SimpleNamespace

        from sqlalchemy.exc import IntegrityError as SA_IntegrityError

        from fonely.services.inventory import _is_unique_violation

        driver = SimpleNamespace(
            sqlstate="23505",
            constraint_name="some_other_constraint",
            __cause__=None,
        )
        exc = SA_IntegrityError(
            'duplicate key value violates unique constraint "uq_inv_op_idempotency"',
            {},
            Exception("inner"),
        )
        exc.orig = driver  # type: ignore[assignment]
        assert _is_unique_violation(exc, "uq_inv_op_idempotency") is False

    def test_missing_diagnostic_reraises(self) -> None:
        from fonely.services.inventory import _is_unique_violation

        exc = self._make_exc(constraint_name=None)
        assert _is_unique_violation(exc, "uq_inv_op_idempotency") is False

    def test_missing_orig_reraises(self) -> None:
        from sqlalchemy.exc import IntegrityError as SA_IntegrityError

        from fonely.services.inventory import _is_unique_violation

        exc = SA_IntegrityError("test", {}, Exception("inner"))
        exc.orig = None  # type: ignore[assignment]
        assert _is_unique_violation(exc, "uq_inv_op_idempotency") is False

    def test_wrong_sqlstate_reraises(self) -> None:
        from fonely.services.inventory import _is_unique_violation

        exc = self._make_exc(sqlstate="23503")
        assert _is_unique_violation(exc, "uq_inv_op_idempotency") is False

    def test_asyncpg_cause_chain_recovers(self) -> None:
        from fonely.services.inventory import _is_unique_violation

        exc = self._make_exc(use_cause_chain=True)
        assert _is_unique_violation(exc, "uq_inv_op_idempotency") is True

    def test_cause_chain_wrong_constraint_reraises(self) -> None:
        from fonely.services.inventory import _is_unique_violation

        exc = self._make_exc(constraint_name="wrong_constraint", use_cause_chain=True)
        assert _is_unique_violation(exc, "uq_inv_op_idempotency") is False


# =============================================================================
# Savepoint uniqueness recovery (controlled boundary test)
# =============================================================================


class TestSavepointUniquenessRecovery:
    """Exercises the IntegrityError → winner-reread recovery path by injecting
    a real SQLSTATE/constraint_name diagnostic at the insert_operation boundary."""

    @staticmethod
    def _make_unique_violation() -> IntegrityError:
        from types import SimpleNamespace

        from sqlalchemy.exc import IntegrityError as SA_IntegrityError

        driver = SimpleNamespace(
            sqlstate="23505",
            constraint_name="uq_inv_op_idempotency",
            __cause__=None,
        )
        exc = SA_IntegrityError(
            "duplicate key value violates unique constraint",
            {},
            Exception("inner"),
        )
        exc.orig = driver  # type: ignore[assignment]
        return exc

    @staticmethod
    def _make_wrong_unique_violation() -> IntegrityError:
        from types import SimpleNamespace

        from sqlalchemy.exc import IntegrityError as SA_IntegrityError

        driver = SimpleNamespace(
            sqlstate="23505",
            constraint_name="uq_some_other_constraint",
            __cause__=None,
        )
        exc = SA_IntegrityError(
            "duplicate key value violates unique constraint",
            {},
            Exception("inner"),
        )
        exc.orig = driver  # type: ignore[assignment]
        return exc

    async def test_exact_constraint_triggers_replay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        authorization_allowed(monkeypatch)
        state = state_with_stock(on_hand="10")
        service = service_for(state)
        winner_digest = DirectInventoryRequestSignature(
            business_id=1,
            operation="set",
            product_id=1,
            quantity=Decimal("5"),
            occurred_at=NOW,
            note=None,
        ).digest
        winner_movement = SimpleNamespace(
            id=1,
            business_id=1,
            product_id=1,
            business_date=BUSINESS_DATE,
            movement_type=InventoryMovementType.MANUAL_ADJUSTMENT.value,
            on_hand_delta=Decimal("5"),
            reserved_delta=Decimal("0"),
            on_hand_after=Decimal("5"),
            reserved_after=Decimal("0"),
            available_after=Decimal("5"),
        )
        state.movements[1] = winner_movement
        winner_op = SimpleNamespace(
            id=99,
            business_id=1,
            idempotency_key="race-key",
            operation="set",
            product_id=1,
            request_digest=winner_digest,
            movement_id=1,
        )

        lookup_count = 0
        insert_count = 0

        async def staged_lookup(
            self_repo: object, business_id: int, idempotency_key: str
        ) -> SimpleNamespace | None:
            nonlocal lookup_count
            lookup_count += 1
            if lookup_count <= 2:
                return None
            return winner_op

        async def insert_then_fail(self_repo: object, values: dict[str, Any]) -> SimpleNamespace:
            nonlocal insert_count
            insert_count += 1
            raise TestSavepointUniquenessRecovery._make_unique_violation()

        monkeypatch.setattr(FakeInventoryRepository, "get_operation_by_key", staged_lookup)
        monkeypatch.setattr(FakeInventoryRepository, "insert_operation", insert_then_fail)

        result = await service.set_stock(
            SetOwnerStockCommand(
                actor=actor(),
                product_id=1,
                quantity="5",
                occurred_at=NOW,
                idempotency_key="race-key",
            )
        )
        assert result.idempotent_replay is True
        assert result.movement_id == 1
        assert insert_count == 1
        assert lookup_count == 3

    async def test_wrong_constraint_is_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        authorization_allowed(monkeypatch)
        state = state_with_stock(on_hand="10")
        service = service_for(state)

        async def insert_wrong_violation(
            self_repo: object, values: dict[str, Any]
        ) -> SimpleNamespace:
            raise TestSavepointUniquenessRecovery._make_wrong_unique_violation()

        monkeypatch.setattr(FakeInventoryRepository, "insert_operation", insert_wrong_violation)

        with pytest.raises(IntegrityError):
            await service.set_stock(
                SetOwnerStockCommand(
                    actor=actor(),
                    product_id=1,
                    quantity="5",
                    occurred_at=NOW,
                    idempotency_key="wrong-constraint-key",
                )
            )

    async def test_changed_digest_after_recovery_raises_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        authorization_allowed(monkeypatch)
        state = state_with_stock(on_hand="10")
        service = service_for(state)
        winner_op = SimpleNamespace(
            id=99,
            business_id=1,
            idempotency_key="conflict-race-key",
            operation="set",
            product_id=1,
            request_digest="different_digest_from_winner",
            movement_id=1,
        )

        lookup_count = 0

        async def staged_lookup(
            self_repo: object, business_id: int, idempotency_key: str
        ) -> SimpleNamespace | None:
            nonlocal lookup_count
            lookup_count += 1
            if lookup_count <= 2:
                return None
            return winner_op

        async def insert_unique_fail(self_repo: object, values: dict[str, Any]) -> SimpleNamespace:
            raise TestSavepointUniquenessRecovery._make_unique_violation()

        monkeypatch.setattr(FakeInventoryRepository, "get_operation_by_key", staged_lookup)
        monkeypatch.setattr(FakeInventoryRepository, "insert_operation", insert_unique_fail)

        with pytest.raises(InventoryIdempotencyConflictError, match="conflicts"):
            await service.set_stock(
                SetOwnerStockCommand(
                    actor=actor(),
                    product_id=1,
                    quantity="5",
                    occurred_at=NOW,
                    idempotency_key="conflict-race-key",
                )
            )
        assert lookup_count == 3
