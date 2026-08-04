"""Background notification delivery with durable provider-attempt evidence."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.models.enums import NotificationStatus
from fonely.models.schema import (
    NotificationOutboxEvent,
    WhatsAppDeliveryAttempt,
)
from fonely.repositories.inbound_events import InboundEventRepository
from fonely.repositories.notifications import NotificationRepository
from fonely.services.whatsapp_notification_sender import (
    DeliveryReceipt,
    NotificationDeliveryError,
)

logger = logging.getLogger("fonely.workers.notification")
BACKOFF_SECONDS = (30, 60, 120, 300, 600)


class NotificationSender(Protocol):
    async def send(self, event: NotificationOutboxEvent) -> DeliveryReceipt: ...


class LoggingNotificationSender:
    """Explicit test/development sender; production startup never selects it."""

    async def send(self, event: NotificationOutboxEvent) -> DeliveryReceipt:
        logger.info(
            "notification_logged",
            extra={"event_id": event.id, "event_type": event.event_type},
        )
        return DeliveryReceipt(provider_message_id=f"logged-{event.id}")


@dataclass(frozen=True)
class ClaimedNotification:
    event_id: int
    business_id: int
    event_type: str
    entity_type: str
    entity_id: int
    recipient_phone: str
    payload: object
    attempts: int
    max_attempts: int


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
        processed = 0
        for _ in range(batch_size):
            claimed = await _claim_one(session_factory)
            if claimed is None:
                break
            await _deliver_claimed(session_factory, sender, claimed)
            processed += 1
        if processed == 0 and max_iterations is None:
            await asyncio.sleep(poll_interval)


async def _deliver_claimed(
    session_factory: async_sessionmaker[AsyncSession],
    sender: NotificationSender,
    claimed: ClaimedNotification,
) -> None:
    event = _snapshot_to_model(claimed)
    attempt_number = claimed.attempts + 1
    await _record_attempt(session_factory, claimed, attempt_number, "sending")
    try:
        receipt = await sender.send(event)
    except NotificationDeliveryError as exc:
        await _record_send_failure(
            session_factory,
            claimed,
            attempt_number,
            exc,
        )
    except Exception as exc:
        await _record_send_failure(
            session_factory,
            claimed,
            attempt_number,
            NotificationDeliveryError(type(exc).__name__),
        )
    else:
        await _record_accepted(
            session_factory,
            claimed,
            attempt_number,
            receipt,
        )


async def _claim_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> ClaimedNotification | None:
    async with session_factory() as session:
        event = await NotificationRepository(session).claim_pending_events(limit=1)
        if not event:
            await session.commit()
            return None
        item = event[0]
        claimed = ClaimedNotification(
            event_id=item.id,
            business_id=item.business_id,
            event_type=item.event_type,
            entity_type=item.entity_type,
            entity_id=item.entity_id,
            recipient_phone=item.recipient_phone,
            payload=item.payload,
            attempts=item.attempts,
            max_attempts=item.max_attempts,
        )
        await session.commit()
        return claimed


def _snapshot_to_model(claimed: ClaimedNotification) -> NotificationOutboxEvent:
    event = NotificationOutboxEvent(
        id=claimed.event_id,
        business_id=claimed.business_id,
        event_type=claimed.event_type,
        entity_type=claimed.entity_type,
        entity_id=claimed.entity_id,
        recipient_type="patient",
        recipient_phone=claimed.recipient_phone,
        channel="whatsapp",
        payload=claimed.payload,
        status=NotificationStatus.PROCESSING.value,
        attempts=claimed.attempts,
        max_attempts=claimed.max_attempts,
        idempotency_key=f"snapshot-{claimed.event_id}",
    )
    return event


async def _record_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedNotification,
    attempt_number: int,
    status: str,
) -> None:
    async with session_factory() as session:
        await session.execute(
            pg_insert(WhatsAppDeliveryAttempt)
            .values(
                business_id=claimed.business_id,
                notification_event_id=claimed.event_id,
                attempt_number=attempt_number,
                status=status,
            )
            .on_conflict_do_nothing(constraint="uq_whatsapp_delivery_attempt")
        )
        await session.commit()


async def _record_accepted(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedNotification,
    attempt_number: int,
    receipt: DeliveryReceipt,
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(WhatsAppDeliveryAttempt)
            .where(
                WhatsAppDeliveryAttempt.business_id == claimed.business_id,
                WhatsAppDeliveryAttempt.notification_event_id == claimed.event_id,
                WhatsAppDeliveryAttempt.attempt_number == attempt_number,
            )
            .values(
                status="accepted",
                provider_message_id=receipt.provider_message_id,
                updated_at=datetime.now(UTC),
            )
        )
        await NotificationRepository(session).mark_delivered(
            claimed.event_id,
            datetime.now(UTC),
        )
        if _is_inbound_response(claimed):
            await InboundEventRepository(session).mark_completed(
                claimed.business_id,
                claimed.entity_id,
                datetime.now(UTC),
            )
        await session.commit()


async def _record_send_failure(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedNotification,
    attempt_number: int,
    exc: NotificationDeliveryError,
) -> None:
    async with session_factory() as session:
        if exc.ambiguous:
            attempt_status = "unknown"
            outbox_status = NotificationStatus.UNKNOWN.value
            next_at = None
        else:
            dead = attempt_number >= claimed.max_attempts
            attempt_status = "failed"
            outbox_status = (
                NotificationStatus.DEAD_LETTER.value if dead else NotificationStatus.FAILED.value
            )
            next_at = None if dead else _next_attempt_at(claimed.attempts)

        await session.execute(
            update(WhatsAppDeliveryAttempt)
            .where(
                WhatsAppDeliveryAttempt.business_id == claimed.business_id,
                WhatsAppDeliveryAttempt.notification_event_id == claimed.event_id,
                WhatsAppDeliveryAttempt.attempt_number == attempt_number,
            )
            .values(
                status=attempt_status,
                error_class=exc.error[:100],
                updated_at=datetime.now(UTC),
            )
        )
        await session.execute(
            update(NotificationOutboxEvent)
            .where(
                NotificationOutboxEvent.business_id == claimed.business_id,
                NotificationOutboxEvent.id == claimed.event_id,
                NotificationOutboxEvent.status == NotificationStatus.PROCESSING.value,
            )
            .values(
                status=outbox_status,
                attempts=attempt_number,
                last_error=exc.error[:500],
                next_attempt_at=next_at,
            )
        )
        if outbox_status == NotificationStatus.DEAD_LETTER.value and _is_inbound_response(claimed):
            await InboundEventRepository(session).mark_response_failed(
                claimed.business_id,
                claimed.entity_id,
            )
        await session.commit()


def _is_inbound_response(event: ClaimedNotification) -> bool:
    return (
        event.event_type == "whatsapp_inbound_response"
        and event.entity_type == "whatsapp_inbound_event"
    )
