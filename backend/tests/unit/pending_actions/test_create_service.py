"""Create/idempotency application-service tests without PostgreSQL."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from fonely.domain.pending_actions.commands import ActorContext, CreatePendingActionCommand
from fonely.domain.pending_actions.errors import PendingActionIdempotencyConflictError
from fonely.models.enums import CallerRole, Channel, PendingActionType
from fonely.models.schema import PendingAction
from fonely.services.pending_actions import PendingActionService

NOW = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)


def actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
        session_id="session-1",
    )


def payload(quantity: str = "2.00") -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_phone": "+919123456789",
            "pickup_at": "2026-08-01T10:00:00Z",
            "lines": [{"product_id": 7, "quantity": quantity}],
        },
    }


def command(quantity: str = "2.00") -> CreatePendingActionCommand:
    return CreatePendingActionCommand(
        actor=actor(),
        action_type=PendingActionType.ORDER,
        payload_schema_version=1,
        payload=payload(quantity),
        expires_at=NOW + timedelta(minutes=15),
        idempotency_key="key-1",
    )


class CreateRepo:
    def __init__(self) -> None:
        self.current: PendingAction | None = None
        self.next_id = 1

    async def get_by_idempotency_key(
        self,
        business_id: int,
        key: str,
    ) -> PendingAction | None:
        if (
            self.current is not None
            and self.current.business_id == business_id
            and self.current.idempotency_key == key
        ):
            return self.current
        return None

    async def insert_idempotent(
        self,
        values: dict[str, object],
    ) -> PendingAction | None:
        if self.current is not None:
            return None
        self.current = PendingAction(
            id=self.next_id,
            created_at=NOW,
            updated_at=NOW,
            **values,
        )
        return self.current


@pytest.fixture
def service() -> tuple[PendingActionService, CreateRepo]:
    instance = PendingActionService(AsyncMock())
    repo = CreateRepo()
    instance._repo = repo  # type: ignore[assignment]
    return instance, repo


@pytest.fixture(autouse=True)
def dependencies() -> object:
    with (
        patch.object(PendingActionService, "_require_business", new=AsyncMock(return_value=None)),
        patch.object(
            PendingActionService,
            "_validate_new_payload_products",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            PendingActionService,
            "_validate_stored_payload_ownership",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "fonely.services.pending_actions.require_action_permission",
            new=AsyncMock(return_value=None),
        ),
        patch("fonely.services.pending_actions.utcnow", return_value=NOW),
    ):
        yield


async def test_create_sets_initial_state_and_version(
    service: tuple[PendingActionService, CreateRepo],
) -> None:
    instance, _ = service
    result = await instance.create(command())
    assert result.id == 1
    assert result.status.value == "collecting_details"
    assert result.version == 1
    assert result.confirmation_snapshot is None


async def test_equivalent_create_returns_existing(
    service: tuple[PendingActionService, CreateRepo],
) -> None:
    instance, _ = service
    first = await instance.create(command())
    second = await instance.create(command())
    assert first.id == second.id


async def test_same_key_different_payload_conflicts(
    service: tuple[PendingActionService, CreateRepo],
) -> None:
    instance, _ = service
    await instance.create(command("2.00"))
    with pytest.raises(PendingActionIdempotencyConflictError):
        await instance.create(command("3.00"))


async def test_same_key_different_expiry_conflicts(
    service: tuple[PendingActionService, CreateRepo],
) -> None:
    instance, _ = service
    await instance.create(command())
    changed = command().model_copy(update={"expires_at": NOW + timedelta(minutes=20)})
    with pytest.raises(PendingActionIdempotencyConflictError):
        await instance.create(changed)


async def test_exact_retry_returns_existing_even_after_expiry(
    service: tuple[PendingActionService, CreateRepo],
) -> None:
    instance, _ = service
    request = command()
    first = await instance.create(request)
    with patch(
        "fonely.services.pending_actions.utcnow",
        return_value=NOW + timedelta(minutes=20),
    ):
        second = await instance.create(request)
    assert second.id == first.id
