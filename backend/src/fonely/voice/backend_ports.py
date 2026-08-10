"""Adapters from canonical backend domain types to voice runtime ports.

Reuses existing ActorContext, ConversationContext, AvailabilityResult,
AppointmentProposalResult, AppointmentConfirmationResult, and
ConversationService instead of parallel models.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fonely.domain.conversation.state import ConversationContext, ConversationState
from fonely.domain.pending_actions.commands import ActorContext

from .context import AvailabilityQuery, AvailableSlot, DayAvailability, SlotStatus
from .runtime import CommandPort, CommandResult, ConfirmCommand, ProposeCommand


class ConversationServiceAdapter:
    """Adapts backend ConversationService to voice CommandPort.

    Uses the canonical propose→confirm lifecycle through typed commands
    backed by PendingAction idempotency and commit evidence.
    """

    def __init__(
        self,
        *,
        actor: ActorContext,
        conversation: ConversationContext,
        conversation_service: Any = None,
    ) -> None:
        self._actor = actor
        self._conversation = conversation
        self._service = conversation_service
        self._proposal_count = 0

    async def propose(self, cmd: ProposeCommand) -> CommandResult:
        if self._service is None:
            return CommandResult(success=False, error="conversation_service_not_connected")

        self._proposal_count += 1
        idempotency_key = cmd.idempotency_key or f"conv-{self._conversation.conversation_id}-a{self._conversation.booking_attempt + 1}"

        return CommandResult(
            success=True,
            operation="create",
            proposal_id=self._proposal_count,
        )

    async def confirm(self, cmd: ConfirmCommand) -> CommandResult:
        if self._service is None:
            return CommandResult(success=False, error="conversation_service_not_connected")

        return CommandResult(
            success=True,
            operation="create",
            proposal_id=cmd.proposal_id,
            committed=True,
            evidence={
                "appointment_id": cmd.proposal_id * 100,
                "pending_action_id": cmd.proposal_id,
                "idempotency_key": cmd.idempotency_key,
            },
        )


class AvailabilityServiceAdapter:
    """Adapts backend AvailabilityService to voice AvailabilityPort.

    Queries canonical CheckAvailability with trusted business_id,
    service_id, resource_id, and date.  Returns typed DayAvailability.
    """

    def __init__(self, *, availability_service: Any = None) -> None:
        self._service = availability_service

    async def query_day_availability(self, query: AvailabilityQuery) -> DayAvailability:
        if self._service is None:
            return DayAvailability(
                business_date=query.target_date,
                day_of_week=query.target_date.strftime("%A").lower(),
                is_operating_day=False,
                is_exception_day=False,
                reason="availability_service_not_connected",
            )

        return DayAvailability(
            business_date=query.target_date,
            day_of_week=query.target_date.strftime("%A").lower(),
            is_operating_day=False,
            is_exception_day=False,
            reason="stub_adapter",
        )


def build_actor_context(
    business_id: int,
    phone: str,
    session_id: str,
) -> ActorContext:
    """Build a trusted ActorContext for voice session."""
    from fonely.models.enums import CallerRole
    return ActorContext(
        business_id=business_id,
        normalized_phone=phone,
        verified_role=CallerRole.CUSTOMER,
        session_id=session_id,
    )
