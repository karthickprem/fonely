"""Application-level inbound call intake — owns the transaction for persist-before-200.

Provider-neutral: accepts InboundCallEvent, not Exotel DTO.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.domain.calls.intake import (
    ConflictingCallEventError,
    DuplicateCallEventError,
    InboundCallEvent,
    InboundCallEventRecord,
)
from fonely.repositories.exotel_intake import InboundCallEventRepository


class InboundCallIntakeService:
    """Transaction-owning intake for the adapter's persist-before-200 contract."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def persist(
        self,
        business_id: int,
        event: InboundCallEvent,
    ) -> InboundCallEventRecord:
        async with self._factory() as session:
            repo = InboundCallEventRepository(session)
            try:
                record = await repo.persist(business_id, event)
                await session.commit()
                return record
            except (DuplicateCallEventError, ConflictingCallEventError):
                await session.rollback()
                raise
            except Exception:
                await session.rollback()
                raise
