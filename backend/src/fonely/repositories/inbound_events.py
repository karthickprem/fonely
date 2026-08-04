"""Tenant-scoped repository for WhatsApp inbound event queue."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.enums import InboundEventStatus
from fonely.models.schema import WhatsAppInboundEvent

BACKOFF_SECONDS = (30, 60, 120, 300, 600)


def _next_attempt_at(attempts: int) -> datetime:
    index = min(attempts, len(BACKOFF_SECONDS) - 1)
    return datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[index])


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
        await self._session.flush()
        return results

    async def mark_domain_processed(
        self, business_id: int, event_id: int
    ) -> None:
        await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.business_id == business_id,
            )
            .values(status=InboundEventStatus.DOMAIN_PROCESSED.value)
        )

    async def mark_completed(
        self, business_id: int, event_id: int, completed_at: datetime
    ) -> None:
        await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.business_id == business_id,
            )
            .values(
                status=InboundEventStatus.COMPLETED.value,
                completed_at=completed_at,
                message_body=None,
            )
        )

    async def mark_failed(
        self, business_id: int, event_id: int, error: str
    ) -> None:
        event = await self._session.scalar(
            select(WhatsAppInboundEvent).where(
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.business_id == business_id,
            )
        )
        if event is None:
            return
        new_attempts = event.attempts + 1
        is_dead = new_attempts >= event.max_attempts
        new_status = (
            InboundEventStatus.DEAD_LETTER.value
            if is_dead
            else InboundEventStatus.FAILED.value
        )
        values: dict[str, object] = {
            "status": new_status,
            "attempts": new_attempts,
            "last_error": error[:500],
            "next_attempt_at": _next_attempt_at(new_attempts - 1),
        }
        if is_dead:
            values["dead_lettered_at"] = datetime.now(UTC)
        await self._session.execute(
            update(WhatsAppInboundEvent)
            .where(
                WhatsAppInboundEvent.id == event_id,
                WhatsAppInboundEvent.business_id == business_id,
            )
            .values(**values)
        )
