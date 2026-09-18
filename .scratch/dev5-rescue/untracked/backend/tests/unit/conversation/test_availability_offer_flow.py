"""Conversation flow tests for selecting persisted offered slots."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from fonely.domain.appointments.results import AppointmentProposalResult, ConfirmationFactsResult
from fonely.domain.conversation.state import ConversationContext, ConversationState
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.services.availability import AvailabilityDecision, AvailabilityReason, AvailableSlot
from fonely.services.conversation import _CONVERSATIONS, ConversationService
from fonely.services.conversation_tools import BusinessContext, ResourceInfo, ServiceInfo
from fonely.services.model_gateway import ModelResponse


def _session() -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    return session


def _actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
    )


def _business() -> BusinessContext:
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


def _proposal(start_at: datetime) -> AppointmentProposalResult:
    return AppointmentProposalResult(
        pending_action_id=42,
        version=1,
        expires_at=start_at + timedelta(minutes=15),
        confirmation_facts=ConfirmationFactsResult(
            operation="create",
            service_id=1,
            service_name="Consultation",
            resource_id=1,
            resource_name="Dr. Priya",
            start_at=start_at.isoformat(),
            end_at=(start_at + timedelta(minutes=30)).isoformat(),
            duration_minutes=30,
            price="300",
            business_timezone="Asia/Kolkata",
        ),
    )


@pytest.fixture(autouse=True)
def _clear_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    _CONVERSATIONS.clear()

    async def no_persist(self: object, conversation_id: str, turn: object) -> None:
        pass

    monkeypatch.setattr(ConversationService, "_persist_turn", no_persist)


async def test_offered_time_preserves_date_and_advances_to_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = datetime(2099, 8, 10, 10, 0, tzinfo=UTC)
    offered = datetime(2099, 8, 10, 11, 30, tzinfo=UTC)  # 5:00 PM Asia/Kolkata
    checks = [
        AvailabilityDecision(
            False,
            AvailabilityReason.CAPACITY_CONFLICT,
            (AvailableSlot(offered, offered + timedelta(minutes=30), 1, "Dr. Priya"),),
        ),
        AvailabilityDecision(True, AvailabilityReason.AVAILABLE),
    ]

    async def check_slot(*args: object, **kwargs: object) -> AvailabilityDecision:
        return checks.pop(0)

    monkeypatch.setattr(
        "fonely.services.conversation_tools.get_business_context",
        AsyncMock(return_value=_business()),
    )
    monkeypatch.setattr(
        "fonely.services.availability.AvailabilityService.check_exact_slot", check_slot
    )
    monkeypatch.setattr(
        "fonely.repositories.appointments.AppointmentRepository.lock_resource_schedule",
        AsyncMock(),
    )

    gateway = AsyncMock()
    gateway.complete.return_value = ModelResponse(text="unused")
    appointments = AsyncMock()
    appointments.create_proposal.return_value = _proposal(offered)
    service = ConversationService(_session(), gateway, appointment_service=appointments)

    ctx = ConversationContext(conversation_id="offer-flow", business_id=1)
    ctx.state = ConversationState.FACT_COLLECTION
    ctx.collected_facts = {
        "_operation": "book",
        "service_id": 1,
        "service_name": "Consultation",
        "resource_id": 1,
        "resource_name": "Dr. Priya",
        "customer_phone": "+919123456789",
        "start_at": requested,
    }
    _CONVERSATIONS[ctx.conversation_id] = ctx

    unavailable = await service.process_message(
        ctx.conversation_id, 1, _actor(), "Is 3:30 PM available today?"
    )
    assert unavailable.assistant_response == (
        "That exact time isn't available. Nearest slots: 5:00 PM. Which one works?"
    )
    assert ctx.collected_facts["start_at"] == requested
    assert ctx.availability_offer is not None

    selected = await service.process_message(ctx.conversation_id, 1, _actor(), "5:00 PM")

    assert selected.state == ConversationState.AWAITING_CONFIRMATION
    assert "5:00 PM" in selected.assistant_response
    command = appointments.create_proposal.call_args.args[0]
    assert command.start_at == offered
    assert command.resource_id == 1
    assert ctx.availability_offer is None
    assert ctx.selected_slot_ref is None


async def test_unmatched_offer_choice_does_not_reparse_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offered = datetime(2099, 8, 10, 11, 30, tzinfo=UTC)
    monkeypatch.setattr(
        "fonely.services.conversation_tools.get_business_context",
        AsyncMock(return_value=_business()),
    )

    from fonely.services.availability_offers import create_availability_offer

    ctx = ConversationContext(conversation_id="offer-no-match", business_id=1)
    ctx.state = ConversationState.FACT_COLLECTION
    ctx.collected_facts = {
        "service_id": 1,
        "resource_id": 1,
        "customer_phone": "+919123456789",
        "start_at": datetime(2099, 8, 10, 10, 0, tzinfo=UTC),
    }
    ctx.availability_offer = create_availability_offer(
        business_id=1,
        conversation_id=ctx.conversation_id,
        service_id=1,
        business_timezone="Asia/Kolkata",
        alternatives=(AvailableSlot(offered, offered + timedelta(minutes=30), 1, "Dr. Priya"),),
        now=datetime(2099, 8, 9, 10, 0, tzinfo=UTC),
    )
    original = ctx.collected_facts["start_at"]
    _CONVERSATIONS[ctx.conversation_id] = ctx

    gateway = AsyncMock()
    gateway.complete.return_value = ModelResponse(text="unused")
    service = ConversationService(_session(), gateway, appointment_service=AsyncMock())
    turn = await service.process_message(ctx.conversation_id, 1, _actor(), "4:00 PM")

    assert "Please choose one of these offered slots" in turn.assistant_response
    assert ctx.collected_facts["start_at"] == original
    assert ctx.availability_offer is not None
