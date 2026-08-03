"""Repository for WhatsApp inbound event queue."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.enums import InboundEventStatus
from fonely.models.schema import WhatsAppInboundEvent


class InboundEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_pending_events(
        self, limit: int = 10, now: datetime | None = None
    ) -> Sequence[WhatsAppInboundEvent]:
        current = now or func.now()
        statement = (
            select(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.status.in_(
                    [InboundEventStatus.RECEIVED.value, InboundEventStatus.FAILED.value]
                ),
                WhatsAppInboundEvent.attempts < WhatsAppInboundEvent.max_attempts,
                (WhatsAppInboundEvent.next_attempt_at <= current)
                | (WhatsAppInboundEvent.next_attempt_at.is_(None)),
            )
            .order_by(WhatsAppInboundEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        results = (await self._session.scalars(statement)).all()
        for event in results:
            event.status = InboundEventStatus.PROCESSING.value
            event.attempts = event.attempts + 1
        await self._session.flush()
        return results

    async def mark_completed(self, event_id: int, completed_at: datetime) -> None:
        await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(WhatsAppInboundEvent.id == event_id)
            .values(
                status=InboundEventStatus.COMPLETED.value,
                completed_at=completed_at,
                message_body=None,
            )
        )

    async def mark_failed(self, event_id: int, error: str, next_attempt_at: datetime) -> None:
        event = await self._session.get(WhatsAppInboundEvent, event_id)
        if event is None:
            return
        new_status = (
            InboundEventStatus.DEAD_LETTER.value
            if event.attempts >= event.max_attempts
            else InboundEventStatus.FAILED.value
        )
        await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(WhatsAppInboundEvent.id == event_id)
            .values(
                status=new_status,
                last_error=error[:500],
                next_attempt_at=next_attempt_at,
            )
        )
