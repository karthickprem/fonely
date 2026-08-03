"""Bridges in-memory ConversationContext with PostgreSQL persistence."""

import hashlib
import logging
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.validators import utcnow
from fonely.domain.conversation.state import (
    ConversationContext,
    ConversationState,
    ConversationTurn,
)
from fonely.repositories.conversations import ConversationRepository

logger = logging.getLogger("fonely.services.conversation_persistence")

_CONVERSATION_TTL = timedelta(hours=1)


class ConversationPersistenceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ConversationRepository(session)

    async def load_or_create(self, business_id: int, customer_phone: str) -> ConversationContext:
        db_conv = await self._repo.get_active_by_phone(business_id, customer_phone)
        if db_conv is not None:
            return self._to_context(db_conv)

        ctx = ConversationContext(business_id=business_id)
        now = utcnow()
        await self._repo.create(
            {
                "id": ctx.conversation_id,
                "business_id": business_id,
                "customer_phone": customer_phone,
                "state": ctx.state.value,
                "collected_facts": {},
                "turn_count": 0,
                "expires_at": now + _CONVERSATION_TTL,
            }
        )
        return ctx

    async def save_turn(
        self,
        ctx: ConversationContext,
        turn: ConversationTurn,
    ) -> None:
        facts_for_json = _serialize_facts(ctx.collected_facts)

        await self._repo.update_state(
            ctx.conversation_id,
            state=ctx.state.value,
            collected_facts=facts_for_json,
            proposal_id=ctx.proposal_id,
            proposal_version=ctx.proposal_version,
            turn_count=ctx.turn_count,
        )

        message_hash = hashlib.sha256(turn.user_message.encode()).hexdigest()
        await self._repo.insert_turn(
            {
                "id": str(uuid.uuid4()),
                "conversation_id": ctx.conversation_id,
                "business_id": ctx.business_id,
                "turn_number": ctx.turn_count,
                "state": turn.state.value,
                "intent": turn.intent.value,
                "safety_classification": turn.safety_classification,
                "user_message_hash": message_hash,
                "assistant_response": turn.assistant_response,
                "collected_facts_snapshot": _serialize_facts(turn.collected_facts),
                "missing_facts": turn.missing_facts,
                "proposal_id": turn.proposal_id,
            }
        )

    async def mark_completed(self, conversation_id: str) -> None:
        await self._repo.mark_completed(conversation_id, utcnow())

    async def load_by_id(self, conversation_id: str) -> ConversationContext | None:
        db_conv = await self._repo.get_by_id(conversation_id)
        if db_conv is None:
            return None
        return self._to_context(db_conv)

    async def cleanup_expired(self) -> int:
        return await self._repo.cleanup_expired(utcnow())

    @staticmethod
    def _to_context(db_conv: object) -> ConversationContext:
        raw_facts = dict(getattr(db_conv, "collected_facts", {}) or {})
        facts = _deserialize_facts(raw_facts)
        ctx = ConversationContext(
            conversation_id=getattr(db_conv, "id", ""),
            business_id=getattr(db_conv, "business_id", 0),
            state=ConversationState(getattr(db_conv, "state", "greeting")),
            collected_facts=facts,
            proposal_id=getattr(db_conv, "proposal_id", None),
            proposal_version=getattr(db_conv, "proposal_version", None),
            created_at=getattr(db_conv, "created_at", utcnow()),
        )
        ctx._restored_turn_count = getattr(db_conv, "turn_count", 0)
        return ctx


def _deserialize_facts(facts: dict[str, object]) -> dict[str, object]:
    from datetime import datetime

    result: dict[str, object] = {}
    for key, value in facts.items():
        if isinstance(value, str) and key in ("start_at", "end_at", "expires_at"):
            try:
                result[key] = datetime.fromisoformat(value)
            except (ValueError, TypeError):
                result[key] = value
        else:
            result[key] = value
    return result


def _serialize_facts(facts: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in facts.items():
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result
