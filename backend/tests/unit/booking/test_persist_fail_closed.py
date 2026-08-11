"""Persistence fail-closed for critical conversation state (M1 condition 5).

When a turn carries critical state (a proposal exists, or the turn is
CONFIRMED/COMPLETED) but the conversation row is missing from the database,
the persistence layer must NOT silently proceed as if the turn were saved.
It emits an observable metric so skipped-because-missing is distinguishable
from succeeded, and a genuine save failure re-raises.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from fonely.core.metrics import metrics
from fonely.domain.conversation.state import (
    ConversationContext,
    ConversationIntent,
    ConversationState,
    ConversationTurn,
)
from fonely.services.conversation import _CONVERSATIONS, ConversationService


@pytest.fixture(autouse=True)
def _clear() -> None:
    _CONVERSATIONS.clear()
    metrics.reset()
    yield
    _CONVERSATIONS.clear()
    metrics.reset()


def _service() -> ConversationService:
    svc = ConversationService.__new__(ConversationService)
    svc._session = AsyncMock()
    svc._model = AsyncMock()
    svc._appointment_service = AsyncMock()
    return svc


def _critical_turn(conv_id: str) -> ConversationTurn:
    return ConversationTurn(
        turn_id=str(uuid.uuid4()),
        conversation_id=conv_id,
        business_id=1,
        state=ConversationState.CONFIRMED,
        user_message="yes",
        assistant_response="Confirmed.",
        collected_facts={},
        missing_facts=[],
        proposal_id=42,
        intent=ConversationIntent.UNKNOWN,
        safety_classification="administrative",
    )


async def test_missing_row_with_critical_state_emits_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv_id = "ghost-conversation"
    ctx = ConversationContext(conversation_id=conv_id, business_id=1)
    ctx.proposal_id = 42
    _CONVERSATIONS[conv_id] = ctx

    # Persistence reports the conversation row does not exist.
    async def _exists(_cid: str) -> bool:
        return False

    from fonely.services import conversation_persistence

    monkeypatch.setattr(
        conversation_persistence.ConversationPersistenceService,
        "exists",
        lambda self, cid: _exists(cid),
    )

    svc = _service()
    turn = _critical_turn(conv_id)

    before = metrics.counter_value("conversation_critical_state_unpersisted", {"business_id": "1"})
    await svc._persist_turn(conv_id, turn)
    after = metrics.counter_value("conversation_critical_state_unpersisted", {"business_id": "1"})

    assert after == before + 1, (
        "Missing DB row with critical state must emit the unpersisted metric"
    )


async def test_missing_row_without_critical_state_no_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv_id = "ghost-2"
    ctx = ConversationContext(conversation_id=conv_id, business_id=1)
    _CONVERSATIONS[conv_id] = ctx  # no proposal, non-terminal state

    from fonely.services import conversation_persistence

    monkeypatch.setattr(
        conversation_persistence.ConversationPersistenceService,
        "exists",
        lambda self, cid: _async_false(),
    )

    svc = _service()
    turn = ConversationTurn(
        turn_id=str(uuid.uuid4()),
        conversation_id=conv_id,
        business_id=1,
        state=ConversationState.FACT_COLLECTION,
        user_message="hi",
        assistant_response="Which service?",
        collected_facts={},
        missing_facts=["service_id"],
        proposal_id=None,
        intent=ConversationIntent.UNKNOWN,
        safety_classification="administrative",
    )

    await svc._persist_turn(conv_id, turn)
    assert (
        metrics.counter_value("conversation_critical_state_unpersisted", {"business_id": "1"}) == 0
    )


async def _async_false() -> bool:
    return False


async def test_save_failure_with_critical_state_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv_id = "exists-but-save-fails"
    ctx = ConversationContext(conversation_id=conv_id, business_id=1)
    ctx.proposal_id = 7
    _CONVERSATIONS[conv_id] = ctx

    from fonely.services import conversation_persistence

    async def _exists(self, cid: str) -> bool:
        return True

    monkeypatch.setattr(conversation_persistence.ConversationPersistenceService, "exists", _exists)

    svc = _service()

    # begin_nested raises to simulate a save failure inside the savepoint.
    def _boom() -> None:
        raise RuntimeError("db down")

    svc._session.begin_nested = _boom  # type: ignore[method-assign]

    turn = _critical_turn(conv_id)

    with pytest.raises(RuntimeError):
        await svc._persist_turn(conv_id, turn)

    # Cache invalidated so memory never advances past PostgreSQL.
    assert conv_id not in _CONVERSATIONS
