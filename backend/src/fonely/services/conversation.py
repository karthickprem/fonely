"""Conversation orchestrator for dental appointment booking."""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.conversation.safety import (
    ESCALATION_MEDICAL,
    ESCALATION_URGENT,
    SafetyClassification,
    classify_intent,
)
from fonely.domain.conversation.state import (
    ConversationContext,
    ConversationIntent,
    ConversationState,
    ConversationTurn,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.services.model_gateway import ModelGateway, ModelResponse

logger = logging.getLogger("fonely.services.conversation")

_CONVERSATIONS: dict[str, ConversationContext] = {}


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        model: ModelGateway,
    ) -> None:
        self._session = session
        self._model = model

    async def process_message(
        self,
        conversation_id: str,
        business_id: int,
        actor: ActorContext,
        user_message: str,
    ) -> ConversationTurn:
        ctx = _CONVERSATIONS.get(conversation_id)
        if ctx is None:
            ctx = ConversationContext(
                conversation_id=conversation_id,
                business_id=business_id,
            )
            _CONVERSATIONS[conversation_id] = ctx

        if ctx.at_turn_limit:
            return self._end_turn(
                ctx,
                user_message,
                "We've reached the conversation limit. "
                "Please call the clinic directly for further assistance.",
                ConversationIntent.UNKNOWN,
                "administrative",
            )

        safety = classify_intent(user_message)

        if safety.classification == "urgent_medical":
            return self._escalate_turn(ctx, user_message, safety, ESCALATION_URGENT)

        if safety.classification == "medical":
            return self._escalate_turn(ctx, user_message, safety, ESCALATION_MEDICAL)

        if ctx.state == ConversationState.GREETING:
            ctx.transition(ConversationState.INTENT_RECOGNITION)

        if ctx.state == ConversationState.INTENT_RECOGNITION:
            ctx.transition(ConversationState.FACT_COLLECTION)

        from fonely.services.conversation_tools import get_business_context

        biz = await get_business_context(business_id, self._session)
        if biz is None:
            return self._end_turn(
                ctx, user_message, "Clinic not found.", ConversationIntent.UNKNOWN, "administrative"
            )

        missing = self._identify_missing_facts(ctx)
        response = await self._generate_response(ctx, user_message, biz, missing, safety)

        if missing and ctx.state == ConversationState.FACT_COLLECTION:
            return self._fact_turn(ctx, user_message, response.text, safety, missing)

        return self._fact_turn(ctx, user_message, response.text, safety, missing)

    def _identify_missing_facts(self, ctx: ConversationContext) -> list[str]:
        required = ["service", "resource", "datetime", "customer_phone"]
        return [f for f in required if f not in ctx.collected_facts]

    async def _generate_response(
        self,
        ctx: ConversationContext,
        user_message: str,
        biz: object,
        missing: list[str],
        safety: SafetyClassification,
    ) -> ModelResponse:
        from fonely.services.conversation_tools import BusinessContext

        assert isinstance(biz, BusinessContext)
        services_text = ", ".join(
            f"{s.name} (₹{s.price}, {s.duration_minutes}min)" for s in biz.services
        )
        resources_text = ", ".join(r.name for r in biz.resources)

        system_prompt = (
            f"You are the virtual receptionist for {biz.name}. "
            f"You handle appointment bookings and clinic enquiries. "
            f"Respond in the caller's language (Tamil/English/mixed). "
            f"Use short spoken sentences, not written prose. "
            f"Ask one question at a time. Never invent clinic information. "
            f"Available services: {services_text}. "
            f"Available dentists: {resources_text}. "
        )

        if missing:
            system_prompt += (
                f"The customer still needs to provide: {', '.join(missing)}. "
                f"Ask about ONE missing item naturally."
            )

        history = [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": t.user_message if i % 2 == 0 else t.assistant_response,
            }
            for i, t in enumerate(ctx.turns[-6:])
        ]
        history.append({"role": "user", "content": user_message})

        return await self._model.complete(
            system_prompt=system_prompt,
            messages=history,
            temperature=0.3,
            max_tokens=300,
        )

    def _fact_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        response: str,
        safety: SafetyClassification,
        missing: list[str],
    ) -> ConversationTurn:
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=response,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=missing,
            proposal_id=ctx.proposal_id,
            proposal_version=ctx.proposal_version,
            intent=safety.intent,
            safety_classification=safety.classification,
        )
        ctx.turns.append(turn)
        return turn

    def _escalate_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        safety: SafetyClassification,
        escalation_message: str,
    ) -> ConversationTurn:
        if ctx.can_transition(ConversationState.ESCALATED):
            ctx.transition(ConversationState.ESCALATED)
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=escalation_message,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=[],
            intent=safety.intent,
            safety_classification=safety.classification,
        )
        ctx.turns.append(turn)
        return turn

    def _end_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        response: str,
        intent: ConversationIntent,
        classification: str,
    ) -> ConversationTurn:
        if ctx.can_transition(ConversationState.ENDED):
            ctx.transition(ConversationState.ENDED)
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=response,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=[],
            intent=intent,
            safety_classification=classification,
        )
        ctx.turns.append(turn)
        return turn


def get_conversation(conversation_id: str) -> ConversationContext | None:
    return _CONVERSATIONS.get(conversation_id)


def create_conversation(business_id: int) -> ConversationContext:
    ctx = ConversationContext(business_id=business_id)
    _CONVERSATIONS[ctx.conversation_id] = ctx
    return ctx
