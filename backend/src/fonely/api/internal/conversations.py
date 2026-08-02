"""Internal conversation API routes."""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.api.internal.appointments import (
    _get_correlation_id,
    _trusted_actor,
    _verify_internal_auth,
)
from fonely.services.conversation import ConversationService, create_conversation, get_conversation
from fonely.services.model_gateway import SarvamModelGateway

logger = logging.getLogger("fonely.api.internal.conversations")

router = APIRouter(prefix="/internal/v1", tags=["internal-conversations"])


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_id: int = Field(gt=0)


class ConversationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    state: str
    business_id: int


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)


class TurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str
    conversation_id: str
    state: str
    user_message: str
    assistant_response: str
    collected_facts: dict[str, object]
    missing_facts: list[str]
    intent: str
    safety_classification: str
    proposal_id: int | None = None
    proposal_version: int | None = None


@router.post("/conversations", response_model=ConversationResponse, status_code=201)
async def create_conversation_route(
    body: CreateConversationRequest,
    request: Request,
) -> ConversationResponse:
    _verify_internal_auth(request)
    ctx = create_conversation(body.business_id)
    return ConversationResponse(
        conversation_id=ctx.conversation_id,
        state=ctx.state.value,
        business_id=ctx.business_id,
    )


@router.post("/conversations/{conversation_id}/messages", response_model=TurnResponse)
async def send_message(
    conversation_id: str,
    body: MessageRequest,
    request: Request,
) -> TurnResponse:
    _verify_internal_auth(request)
    actor = _trusted_actor(request)

    ctx = get_conversation(conversation_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            gateway = SarvamModelGateway()
            service = ConversationService(session, gateway)
            turn = await service.process_message(
                conversation_id=conversation_id,
                business_id=ctx.business_id,
                actor=actor,
                user_message=body.message,
            )
            return TurnResponse(
                turn_id=turn.turn_id,
                conversation_id=turn.conversation_id,
                state=turn.state.value,
                user_message=turn.user_message,
                assistant_response=turn.assistant_response,
                collected_facts=turn.collected_facts,
                missing_facts=turn.missing_facts,
                intent=turn.intent.value,
                safety_classification=turn.safety_classification,
                proposal_id=turn.proposal_id,
                proposal_version=turn.proposal_version,
            )
        except Exception as exc:
            logger.warning(
                "conversation_failed",
                extra={
                    "correlation_id": _get_correlation_id(request),
                    "error_type": type(exc).__name__,
                },
            )
            raise HTTPException(status_code=500, detail="Internal error") from None


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation_route(
    conversation_id: str,
    request: Request,
) -> ConversationResponse:
    _verify_internal_auth(request)
    ctx = get_conversation(conversation_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationResponse(
        conversation_id=ctx.conversation_id,
        state=ctx.state.value,
        business_id=ctx.business_id,
    )
