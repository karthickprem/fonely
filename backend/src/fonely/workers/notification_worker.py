"""Background notification delivery worker using PostgreSQL outbox polling."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.models.schema import NotificationOutboxEvent
from fonely.repositories.notifications import NotificationRepository

logger = logging.getLogger("fonely.workers.notification")

BACKOFF_SECONDS = (30, 60, 120, 300, 600)


class NotificationSender(Protocol):
    async def send(self, event: NotificationOutboxEvent) -> None: ...


class LoggingNotificationSender:
    async def send(self, event: NotificationOutboxEvent) -> None:
        logger.info(
            "notification_sent",
            extra={
                "event_id": event.id,
                "event_type": event.event_type,
                "recipient_type": event.recipient_type,
                "recipient_phone": event.recipient_phone,
                "channel": event.channel,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
            },
        )


def _next_attempt_at(attempts: int) -> datetime:
    index = min(attempts, len(BACKOFF_SECONDS) - 1)
    return datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[index])


async def run_notification_worker(
    session_factory: async_sessionmaker[AsyncSession],
    sender: NotificationSender,
    *,
    poll_interval: float = 5.0,
    batch_size: int = 10,
    max_iterations: int | None = None,
) -> None:
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        async with session_factory() as session:
            repo = NotificationRepository(session)
            from fonely.core.metrics import metrics

            events = await repo.claim_pending_events(limit=batch_size)
            for event in events:
                try:
                    await sender.send(event)
                    await repo.mark_delivered(event.id, datetime.now(UTC))
                    metrics.increment("notifications_processed_total", {"outcome": "delivered"})
                except Exception as exc:
                    next_at = _next_attempt_at(event.attempts)
                    await repo.mark_failed(event.id, type(exc).__name__, next_at)
                    outcome = (
                        "dead_letter" if event.attempts + 1 >= event.max_attempts else "failed"
                    )
                    metrics.increment("notifications_processed_total", {"outcome": outcome})
                    metrics.increment("notification_retry_total")
                    logger.warning(
                        "notification_delivery_failed",
                        extra={
                            "event_id": event.id,
                            "error": type(exc).__name__,
                            "attempt": event.attempts + 1,
                        },
                    )
            await session.commit()
        if max_iterations is None:
            await asyncio.sleep(poll_interval)
