"""Tenant-scoped notification outbox persistence."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from fonely.models.enums import NotificationStatus
from fonely.models.schema import NotificationOutboxEvent


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_event(self, values: Mapping[str, Any]) -> NotificationOutboxEvent:
        event = NotificationOutboxEvent(**values)
        self._session.add(event)
        await self._session.flush()
        return event

    async def insert_event_idempotent(
        self, values: Mapping[str, Any]
    ) -> NotificationOutboxEvent | None:
        statement = (
            pg_insert(NotificationOutboxEvent)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_notification_idempotency")
            .returning(NotificationOutboxEvent)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def claim_pending_events(
        self, limit: int = 10, now: datetime | None = None
    ) -> Sequence[NotificationOutboxEvent]:
        current = now or datetime.now(UTC)
        stale_before = (
            current - timedelta(minutes=5)
            if isinstance(current, datetime)
            else datetime.now(UTC) - timedelta(minutes=5)
        )
        statement = (
            select(NotificationOutboxEvent)
            .where(
                (
                    NotificationOutboxEvent.status.in_(
                        [NotificationStatus.PENDING.value, NotificationStatus.FAILED.value]
                    )
                    & (
                        (NotificationOutboxEvent.next_attempt_at <= current)
                        | (NotificationOutboxEvent.next_attempt_at.is_(None))
                    )
                )
                | (
                    (NotificationOutboxEvent.status == NotificationStatus.PROCESSING.value)
                    & (NotificationOutboxEvent.updated_at < stale_before)
                ),
                NotificationOutboxEvent.attempts < NotificationOutboxEvent.max_attempts,
            )
            .order_by(NotificationOutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        results = (await self._session.scalars(statement)).all()
        for event in results:
            event.status = NotificationStatus.PROCESSING.value
            event.updated_at = func.now()
        await self._session.flush()
        return results

    async def mark_delivered(self, event_id: int, delivered_at: datetime) -> None:
        await self._session.execute(
            update(NotificationOutboxEvent)
            .where(NotificationOutboxEvent.id == event_id)
            .values(
                status=NotificationStatus.DELIVERED.value,
                delivered_at=delivered_at,
                updated_at=func.now(),
            )
        )

    async def mark_failed(self, event_id: int, error: str, next_attempt_at: datetime) -> None:
        event = await self._session.get(NotificationOutboxEvent, event_id)
        if event is None:
            return
        new_attempts = event.attempts + 1
        new_status = (
            NotificationStatus.DEAD_LETTER.value
            if new_attempts >= event.max_attempts
            else NotificationStatus.FAILED.value
        )
        await self._session.execute(
            update(NotificationOutboxEvent)
            .where(NotificationOutboxEvent.id == event_id)
            .values(
                status=new_status,
                attempts=new_attempts,
                last_error=error[:500],
                next_attempt_at=next_attempt_at,
                updated_at=func.now(),
            )
        )

    async def get_events_for_entity(
        self, business_id: int, entity_type: str, entity_id: int
    ) -> Sequence[NotificationOutboxEvent]:
        statement = (
            select(NotificationOutboxEvent)
            .where(
                NotificationOutboxEvent.business_id == business_id,
                NotificationOutboxEvent.entity_type == entity_type,
                NotificationOutboxEvent.entity_id == entity_id,
            )
            .order_by(NotificationOutboxEvent.id)
        )
        return (await self._session.scalars(statement)).all()
