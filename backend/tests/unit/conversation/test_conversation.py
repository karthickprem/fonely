"""Unit tests for conversation service with mocked model gateway."""

from unittest.mock import AsyncMock

import pytest

from fonely.domain.conversation.safety import ESCALATION_MEDICAL, ESCALATION_URGENT
from fonely.domain.conversation.state import ConversationState
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.services.conversation import _CONVERSATIONS, ConversationService
from fonely.services.conversation_tools import BusinessContext, ResourceInfo, ServiceInfo
from fonely.services.model_gateway import ModelResponse


def _actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
    )


def _mock_gateway(text: str = "How can I help you?") -> AsyncMock:
    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text=text)
    return gw


def _mock_biz_context() -> BusinessContext:
    return BusinessContext(
        business_id=1,
        name="Smile Dental",
        timezone="Asia/Kolkata",
        services=[
            ServiceInfo(
                id=1,
                name="Consultation",
                duration_minutes=30,
                buffer_before_minutes=0,
                buffer_after_minutes=0,
                price="300",
            )
        ],
        resources=[ResourceInfo(id=1, name="Dr. Priya", resource_type="staff")],
        eligibility=[(1, 1)],
    )


@pytest.fixture(autouse=True)
def clear_conversations() -> None:
    _CONVERSATIONS.clear()


async def test_greeting_transitions_to_fact_collection() -> None:
    session = AsyncMock()
    gateway = _mock_gateway("Welcome! What service would you like?")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fonely.services.conversation_tools.get_business_context",
            AsyncMock(return_value=_mock_biz_context()),
        )
        service = ConversationService(session, gateway)
        turn = await service.process_message(
            "conv-1", 1, _actor(), "Hi, I want to book an appointment"
        )

    assert turn.state == ConversationState.FACT_COLLECTION
    assert turn.safety_classification == "administrative"
    assert turn.assistant_response == "Welcome! What service would you like?"


async def test_medical_question_escalates() -> None:
    session = AsyncMock()
    gateway = _mock_gateway()

    service = ConversationService(session, gateway)
    turn = await service.process_message(
        "conv-2", 1, _actor(), "My tooth has been hurting badly for days"
    )

    assert turn.state == ConversationState.ESCALATED
    assert turn.safety_classification == "medical"
    assert ESCALATION_MEDICAL in turn.assistant_response
    gateway.complete.assert_not_called()


async def test_urgent_medical_escalates_immediately() -> None:
    session = AsyncMock()
    gateway = _mock_gateway()

    service = ConversationService(session, gateway)
    turn = await service.process_message(
        "conv-3", 1, _actor(), "Emergency! There is heavy bleeding from the gum"
    )

    assert turn.state == ConversationState.ESCALATED
    assert turn.safety_classification == "urgent_medical"
    assert ESCALATION_URGENT in turn.assistant_response
    gateway.complete.assert_not_called()


async def test_turn_limit_ends_conversation() -> None:
    session = AsyncMock()
    gateway = _mock_gateway()

    from fonely.domain.conversation.state import MAX_TURNS, ConversationTurn
    from fonely.services.conversation import _CONVERSATIONS, ConversationContext

    ctx = ConversationContext(conversation_id="conv-limit", business_id=1)
    for i in range(MAX_TURNS):
        ctx.turns.append(
            ConversationTurn(
                turn_id=str(i),
                conversation_id="conv-limit",
                business_id=1,
                state=ctx.state,
                user_message="test",
                assistant_response="test",
                collected_facts={},
                missing_facts=[],
            )
        )
    _CONVERSATIONS["conv-limit"] = ctx

    service = ConversationService(session, gateway)
    turn = await service.process_message("conv-limit", 1, _actor(), "hello")

    assert turn.state == ConversationState.ENDED
    assert "limit" in turn.assistant_response.lower()


async def test_missing_business_ends_conversation() -> None:
    session = AsyncMock()
    gateway = _mock_gateway()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fonely.services.conversation_tools.get_business_context",
            AsyncMock(return_value=None),
        )
        service = ConversationService(session, gateway)
        turn = await service.process_message("conv-no-biz", 999, _actor(), "Hello")

    assert turn.state == ConversationState.ENDED
    assert "not found" in turn.assistant_response.lower()


async def test_identifies_missing_facts() -> None:
    session = AsyncMock()
    gateway = _mock_gateway("Which service would you like?")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fonely.services.conversation_tools.get_business_context",
            AsyncMock(return_value=_mock_biz_context()),
        )
        service = ConversationService(session, gateway)
        turn = await service.process_message("conv-facts", 1, _actor(), "I want to book")

    assert len(turn.missing_facts) > 0
    assert "service" in turn.missing_facts
