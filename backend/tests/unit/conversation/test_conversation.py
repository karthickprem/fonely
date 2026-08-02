"""Unit tests for conversation service with mocked model gateway."""

from unittest.mock import AsyncMock

import pytest

from fonely.domain.conversation.safety import ESCALATION_MEDICAL, ESCALATION_URGENT
from fonely.domain.conversation.state import ConversationState
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.services.conversation import (
    _CONVERSATIONS,
    _MAX_CONVERSATIONS,
    ConversationService,
    create_conversation,
)
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
        service = ConversationService(session, gateway, appointment_service=AsyncMock())
        turn = await service.process_message(
            "conv-1", 1, _actor(), "Hi, I want to book an appointment"
        )

    assert turn.state == ConversationState.FACT_COLLECTION
    assert turn.safety_classification == "administrative"


async def test_medical_question_escalates() -> None:
    session = AsyncMock()
    gateway = _mock_gateway()

    service = ConversationService(session, gateway, appointment_service=AsyncMock())
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

    service = ConversationService(session, gateway, appointment_service=AsyncMock())
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

    from fonely.domain.conversation.state import MAX_TURNS, ConversationContext, ConversationTurn

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

    service = ConversationService(session, gateway, appointment_service=AsyncMock())
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
        service = ConversationService(session, gateway, appointment_service=AsyncMock())
        turn = await service.process_message("conv-no-biz", 999, _actor(), "Hello")

    assert turn.state == ConversationState.ENDED
    assert "not found" in turn.assistant_response.lower()


async def test_fact_extraction_populates_service() -> None:
    session = AsyncMock()
    gateway = _mock_gateway("Great choice! When would you like to come?")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fonely.services.conversation_tools.get_business_context",
            AsyncMock(return_value=_mock_biz_context()),
        )
        service = ConversationService(session, gateway, appointment_service=AsyncMock())
        turn = await service.process_message("conv-facts", 1, _actor(), "I want a consultation")

    assert turn.collected_facts.get("service_id") == 1
    assert turn.collected_facts.get("service_name") == "Consultation"


async def test_fact_extraction_populates_resource() -> None:
    session = AsyncMock()
    gateway = _mock_gateway("When would you like to see Dr. Priya?")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fonely.services.conversation_tools.get_business_context",
            AsyncMock(return_value=_mock_biz_context()),
        )
        service = ConversationService(session, gateway, appointment_service=AsyncMock())
        turn = await service.process_message(
            "conv-res", 1, _actor(), "I want to see Dr. Priya for consultation"
        )

    assert turn.collected_facts.get("resource_id") == 1


async def test_confirmation_positive_completes() -> None:
    session = AsyncMock()
    gateway = _mock_gateway()

    from fonely.domain.appointments.results import (
        AppointmentConfirmationResult,
        PreCommitAppointmentSuccess,
    )
    from fonely.domain.conversation.state import ConversationContext

    ctx = ConversationContext(conversation_id="conv-confirm", business_id=1)
    ctx.state = ConversationState.AWAITING_CONFIRMATION
    ctx.proposal_id = 42
    ctx.proposal_version = 3
    _CONVERSATIONS["conv-confirm"] = ctx

    from datetime import UTC, datetime

    mock_appt_service = AsyncMock()
    mock_appt_service.confirm_and_commit.return_value = PreCommitAppointmentSuccess(
        appointment=AppointmentConfirmationResult(
            appointment_id=1,
            pending_action_id=42,
            service_id=1,
            service_name="Consultation",
            resource_id=1,
            resource_name="Dr. Priya",
            start_at=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
            end_at=datetime(2026, 8, 5, 10, 30, tzinfo=UTC),
            price=None,
            business_timezone="Asia/Kolkata",
        ),
        pending_action_version=4,
    )

    service = ConversationService(session, gateway, appointment_service=mock_appt_service)
    turn = await service.process_message("conv-confirm", 1, _actor(), "yes")

    assert turn.state == ConversationState.COMPLETED
    assert "confirmed" in turn.assistant_response.lower()
    mock_appt_service.confirm_and_commit.assert_called_once()


async def test_confirmation_negative_returns_to_facts() -> None:
    session = AsyncMock()
    gateway = _mock_gateway()

    from fonely.domain.conversation.state import ConversationContext

    ctx = ConversationContext(conversation_id="conv-no", business_id=1)
    ctx.state = ConversationState.AWAITING_CONFIRMATION
    ctx.collected_facts = {"service_id": 1, "start_at": "dummy"}
    _CONVERSATIONS["conv-no"] = ctx

    service = ConversationService(session, gateway, appointment_service=AsyncMock())
    turn = await service.process_message("conv-no", 1, _actor(), "no, different time")

    assert turn.state == ConversationState.FACT_COLLECTION
    assert "start_at" not in turn.collected_facts


async def test_confirmation_ambiguous_asks_clarification() -> None:
    session = AsyncMock()
    gateway = _mock_gateway()

    from fonely.domain.conversation.state import ConversationContext

    ctx = ConversationContext(conversation_id="conv-maybe", business_id=1)
    ctx.state = ConversationState.AWAITING_CONFIRMATION
    _CONVERSATIONS["conv-maybe"] = ctx

    service = ConversationService(session, gateway, appointment_service=AsyncMock())
    turn = await service.process_message("conv-maybe", 1, _actor(), "hmm let me think")

    assert turn.state == ConversationState.AWAITING_CONFIRMATION
    assert "yes or no" in turn.assistant_response.lower()


async def test_history_construction_correct() -> None:
    session = AsyncMock()
    gateway = _mock_gateway("Next question")

    from fonely.domain.conversation.state import ConversationContext, ConversationTurn

    ctx = ConversationContext(conversation_id="conv-hist", business_id=1)
    ctx.state = ConversationState.FACT_COLLECTION
    ctx.turns.append(
        ConversationTurn(
            turn_id="1",
            conversation_id="conv-hist",
            business_id=1,
            state=ConversationState.FACT_COLLECTION,
            user_message="Hello",
            assistant_response="Welcome!",
            collected_facts={},
            missing_facts=[],
        )
    )
    _CONVERSATIONS["conv-hist"] = ctx

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fonely.services.conversation_tools.get_business_context",
            AsyncMock(return_value=_mock_biz_context()),
        )
        service = ConversationService(session, gateway, appointment_service=AsyncMock())
        await service.process_message("conv-hist", 1, _actor(), "I need help")

    call_args = gateway.complete.call_args
    messages = call_args.kwargs.get("messages") or call_args[1]
    assert messages[-3]["role"] == "user"
    assert messages[-3]["content"] == "Hello"
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["content"] == "Welcome!"
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "I need help"


async def test_conversation_eviction_at_capacity() -> None:
    from fonely.services.conversation import _CONVERSATIONS

    for _ in range(_MAX_CONVERSATIONS + 5):
        create_conversation(business_id=1)

    assert len(_CONVERSATIONS) <= _MAX_CONVERSATIONS


async def test_concurrent_messages_do_not_corrupt() -> None:
    import asyncio

    session = AsyncMock()
    gateway = _mock_gateway("Response")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fonely.services.conversation_tools.get_business_context",
            AsyncMock(return_value=_mock_biz_context()),
        )
        service = ConversationService(session, gateway, appointment_service=AsyncMock())
        tasks = [
            asyncio.create_task(service.process_message("conv-concurrent", 1, _actor(), f"msg-{i}"))
            for i in range(2)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == 2
    ctx = _CONVERSATIONS.get("conv-concurrent")
    assert ctx is not None
    assert ctx.turn_count == 2


async def test_timeout_returns_graceful_response() -> None:
    session = AsyncMock()
    gateway = AsyncMock()

    async def slow_complete(*args: object, **kwargs: object) -> ModelResponse:
        await asyncio.sleep(60)
        return ModelResponse(text="late")

    gateway.complete = slow_complete

    import asyncio

    from fonely.services.conversation import _TIMEOUT_RESPONSE

    with (
        pytest.MonkeyPatch.context() as mp,
        pytest.MonkeyPatch.context() as mp2,
    ):
        mp.setattr(
            "fonely.services.conversation_tools.get_business_context",
            AsyncMock(return_value=_mock_biz_context()),
        )
        mp2.setattr("fonely.services.conversation.settings.conversation_timeout_seconds", 0.1)
        service = ConversationService(session, gateway, appointment_service=AsyncMock())
        turn = await service.process_message("conv-timeout", 1, _actor(), "Hello")

    assert _TIMEOUT_RESPONSE in turn.assistant_response


async def test_fact_validation_rejects_past_time() -> None:
    session = AsyncMock()
    gateway = _mock_gateway("What time works?")

    from datetime import UTC, datetime

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fonely.services.conversation_tools.get_business_context",
            AsyncMock(return_value=_mock_biz_context()),
        )
        service = ConversationService(session, gateway, appointment_service=AsyncMock())

        from fonely.services.conversation import _CONVERSATIONS

        ctx_id = "conv-past"
        from fonely.domain.conversation.state import ConversationContext

        ctx = ConversationContext(conversation_id=ctx_id, business_id=1)
        ctx.state = ConversationState.FACT_COLLECTION
        ctx.collected_facts = {
            "service_id": 1,
            "resource_id": 1,
            "customer_phone": "+919123456789",
            "start_at": datetime(2020, 1, 1, 10, 0, tzinfo=UTC),
        }
        _CONVERSATIONS[ctx_id] = ctx

        turn = await service.process_message(ctx_id, 1, _actor(), "ok")

    assert "start_at" not in turn.collected_facts


async def test_customer_name_extraction() -> None:
    session = AsyncMock()
    gateway = _mock_gateway("Nice to meet you!")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fonely.services.conversation_tools.get_business_context",
            AsyncMock(return_value=_mock_biz_context()),
        )
        service = ConversationService(session, gateway, appointment_service=AsyncMock())
        turn = await service.process_message("conv-name", 1, _actor(), "My name is Raj Kumar")

    assert turn.collected_facts.get("customer_name") == "Raj Kumar"
