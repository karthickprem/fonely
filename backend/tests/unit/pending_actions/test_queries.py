"""Actor-authorized and expiry-aware pending-action query tests."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from fonely.domain.pending_actions.commands import (
    ActorContext,
    GetActivePendingActionQuery,
    GetPendingActionQuery,
)
from fonely.domain.pending_actions.errors import PendingActionUnauthorizedError
from fonely.domain.pending_actions.payloads import validate_payload
from fonely.domain.pending_actions.snapshots import canonical_payload_dict, payload_digest
from fonely.models.enums import CallerRole, PendingActionStatus, PendingActionType
from fonely.models.schema import PendingAction
from fonely.repositories.pending_actions import PendingActionRepository
from fonely.services.pending_actions import PendingActionService

NOW = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)


def actor(phone: str = "+919123456789") -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone=phone,
        verified_role=CallerRole.CUSTOMER,
        session_id="session-1",
    )


def action(
    status: PendingActionStatus = PendingActionStatus.COLLECTING_DETAILS,
    expires_at: datetime = NOW + timedelta(minutes=15),
) -> PendingAction:
    raw = {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_phone": "+919123456789",
            "pickup_at": "2026-08-01T10:00:00Z",
            "lines": [{"product_id": 7, "quantity": "1.00"}],
        },
    }
    validated = validate_payload(PendingActionType.ORDER, 1, raw)
    return PendingAction(
        id=1,
        business_id=1,
        session_id="session-1",
        action_type="order",
        payload_schema_version=1,
        proposed_payload=canonical_payload_dict(validated),
        payload_digest=payload_digest(validated),
        status=status.value,
        expires_at=expires_at,
        idempotency_key="key",
        initiated_by="+919123456789",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


class QueryRepo:
    def __init__(self, current: PendingAction | None) -> None:
        self.current = current
        self.last_now: datetime | None = None

    async def get_by_id(self, business_id: int, action_id: int) -> PendingAction | None:
        if (
            self.current
            and self.current.business_id == business_id
            and self.current.id == action_id
        ):
            return self.current
        return None

    async def get_active_for_session(
        self,
        business_id: int,
        session_id: str,
        now: datetime,
        action_type: PendingActionType | None = None,
    ) -> PendingAction | None:
        self.last_now = now
        current = self.current
        if (
            current is None
            or current.business_id != business_id
            or current.session_id != session_id
        ):
            return None
        if action_type and current.action_type != action_type.value:
            return None
        if current.status == PendingActionStatus.COMMITTING.value:
            return current
        if (
            current.status
            in {
                PendingActionStatus.COLLECTING_DETAILS.value,
                PendingActionStatus.AWAITING_CONFIRMATION.value,
            }
            and current.expires_at > now
        ):
            return current
        return None


@pytest.fixture(autouse=True)
def dependencies() -> object:
    with patch.object(
        PendingActionService,
        "_validate_stored_payload_ownership",
        new=AsyncMock(return_value=None),
    ):
        yield


async def test_public_get_authorizes_customer_owner() -> None:
    service = PendingActionService(AsyncMock())
    service._repo = QueryRepo(action())  # type: ignore[assignment]
    result = await service.get(GetPendingActionQuery(actor=actor(), action_id=1))
    assert result.id == 1


async def test_public_get_rejects_other_customer() -> None:
    service = PendingActionService(AsyncMock())
    service._repo = QueryRepo(action())  # type: ignore[assignment]
    with pytest.raises(PendingActionUnauthorizedError):
        await service.get(
            GetPendingActionQuery(
                actor=actor("+919876543210"),
                action_id=1,
            )
        )


async def test_get_active_excludes_expired_collecting_action() -> None:
    service = PendingActionService(AsyncMock())
    repo = QueryRepo(action(expires_at=NOW))
    service._repo = repo  # type: ignore[assignment]
    with patch("fonely.services.pending_actions.utcnow", return_value=NOW):
        result = await service.get_active(
            GetActivePendingActionQuery(
                actor=actor(),
                session_id="session-1",
                action_type=PendingActionType.ORDER,
            )
        )
    assert result is None
    assert repo.last_now == NOW


async def test_get_active_keeps_committing_action_after_proposal_expiry() -> None:
    service = PendingActionService(AsyncMock())
    repo = QueryRepo(action(PendingActionStatus.COMMITTING, expires_at=NOW))
    service._repo = repo  # type: ignore[assignment]
    with patch("fonely.services.pending_actions.utcnow", return_value=NOW):
        result = await service.get_active(
            GetActivePendingActionQuery(
                actor=actor(),
                session_id="session-1",
                action_type=PendingActionType.ORDER,
            )
        )
    assert result is not None
    assert result.status == PendingActionStatus.COMMITTING


async def test_repository_active_query_contains_expiry_and_committing_policy() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    repo = PendingActionRepository(session)
    await repo.get_active_for_session(1, "session-1", NOW, PendingActionType.ORDER)
    statement = session.execute.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "pending_actions.expires_at >" in sql
    assert "pending_actions.status =" in sql
    assert " OR " in sql
