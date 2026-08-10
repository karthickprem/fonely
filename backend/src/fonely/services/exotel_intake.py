"""Application-level Exotel intake — owns the transaction for persist-before-200.

Wraps ExotelInboundEventRepository in a session-factory-owned transaction.
The adapter calls this; it commits before returning.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.domain.calls.events import ExotelCallbackEvent
from fonely.domain.calls.intake import (
    ConflictingCallEventError,
    DuplicateCallEventError,
    InboundCallEventRecord,
)
from fonely.domain.calls.transitions import LateCallEventError
from fonely.repositories.exotel_intake import ExotelInboundEventRepository


class ExotelIntakeService:
    """Transaction-owning intake for the adapter's persist-before-200 contract.

    Creates a session, persists via the repository, and commits before
    returning. On duplicate/conflict/late-event, the appropriate typed
    exception propagates to the adapter (which maps each to its HTTP status).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def persist(
        self,
        business_id: int,
        event: ExotelCallbackEvent,
    ) -> InboundCallEventRecord:
        async with self._factory() as session:
            repo = ExotelInboundEventRepository(session)
            try:
                record = await repo.persist(business_id, event)
                await session.commit()
                return record
            except (DuplicateCallEventError, ConflictingCallEventError, LateCallEventError):
                await session.rollback()
                raise
            except Exception:
                await session.rollback()
                raise
