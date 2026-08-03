"""Tenant-scoped conversation persistence."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from fonely.domain.conversation.state import ConversationState
from fonely.models.schema import Conversation, DBConversationTurn

_TERMINAL_STATES = (
    ConversationState.COMPLETED.value,
    ConversationState.ENDED.value,
    ConversationState.ESCALATED.value,
)


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_by_phone(
        self, business_id: int, customer_phone: str
    ) -> Conversation | None:
        statement = (
            select(Conversation)
            .where(
                Conversation.business_id == business_id,
                Conversation.customer_phone == customer_phone,
                Conversation.state.notin_(_TERMINAL_STATES),
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        return (await self._session.scalars(statement)).first()

    async def get_by_id(self, conversation_id: str) -> Conversation | None:
        return await self._session.get(Conversation, conversation_id)

    async def create(self, values: Mapping[str, Any]) -> Conversation:
        conv = Conversation(**values)
        self._session.add(conv)
        await self._session.flush()
        return conv

    async def update_state(
        self,
        conversation_id: str,
        *,
        state: str,
        collected_facts: dict[str, object],
        proposal_id: int | None,
        proposal_version: int | None,
        turn_count: int,
    ) -> Conversation | None:
        statement = (
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                state=state,
                collected_facts=collected_facts,
                proposal_id=proposal_id,
                proposal_version=proposal_version,
                turn_count=turn_count,
                updated_at=func.now(),
            )
            .returning(Conversation)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def mark_completed(self, conversation_id: str, completed_at: datetime) -> None:
        await self._session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                state=ConversationState.COMPLETED.value,
                completed_at=completed_at,
                updated_at=func.now(),
            )
        )

    async def insert_turn(self, values: Mapping[str, Any]) -> DBConversationTurn:
        turn = DBConversationTurn(**values)
        self._session.add(turn)
        await self._session.flush()
        return turn

    async def get_turns(self, conversation_id: str) -> Sequence[DBConversationTurn]:
        statement = (
            select(DBConversationTurn)
            .where(DBConversationTurn.conversation_id == conversation_id)
            .order_by(DBConversationTurn.turn_number)
        )
        return (await self._session.scalars(statement)).all()

    async def cleanup_expired(self, before: datetime) -> int:
        expired = (
            await self._session.scalars(
                select(Conversation.id).where(
                    Conversation.expires_at <= before,
                    Conversation.state.notin_(_TERMINAL_STATES),
                )
            )
        ).all()
        if not expired:
            return 0
        await self._session.execute(
            delete(DBConversationTurn).where(DBConversationTurn.conversation_id.in_(expired))
        )
        await self._session.execute(delete(Conversation).where(Conversation.id.in_(expired)))
        return len(expired)
