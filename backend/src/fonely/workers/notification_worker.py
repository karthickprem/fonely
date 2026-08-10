"""Background notification delivery with durable provider-attempt evidence."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select, update
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
        return DeliveryReceipt(provider_message_id=f"logged-{event.id}", final=True)


@dataclass(frozen=True)
class ClaimedNotification:
    event_id: int
    business_id: int
    event_type: str
    entity_type: str
    entity_id: int
    recipient_type: str
    recipient_phone: str
    channel: str
    payload: object
    attempts: int
    max_attempts: int
    claim_token: uuid.UUID | None = None
    claim_version: int = 0


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
    stop: asyncio.Event | None = None,
) -> None:
    iterations = 0

    def _should_continue() -> bool:
        if stop is not None and stop.is_set():
            return False
        return not (max_iterations is not None and iterations >= max_iterations)

    while _should_continue():
        iterations += 1
        processed = 0
        for _ in range(batch_size):
            if stop is not None and stop.is_set():
                break
            claimed = await _claim_one(session_factory)
            if claimed is None:
                break
            if stop is not None and stop.is_set():
                await _release_notification_claim(session_factory, claimed)
                break
            await _deliver_claimed(session_factory, sender, claimed)
            processed += 1
        if processed == 0 and max_iterations is None:
            if stop is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            else:
                await asyncio.sleep(poll_interval)


async def _deliver_claimed(
    session_factory: async_sessionmaker[AsyncSession],
    sender: NotificationSender,
    claimed: ClaimedNotification,
) -> None:
    event = _snapshot_to_model(claimed)
    attempt_number = claimed.attempts + 1
    created = await _record_attempt(session_factory, claimed, attempt_number, "sending")
    if not created:
        await _record_abandoned_sending(session_factory, claimed, attempt_number)
        return
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


async def _expire_unknown(session: AsyncSession) -> None:
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    events = (
        await session.scalars(
            select(NotificationOutboxEvent).where(
                NotificationOutboxEvent.status == NotificationStatus.UNKNOWN.value,
                NotificationOutboxEvent.updated_at < cutoff,
            )
        )
    ).all()
    inbound_repo = InboundEventRepository(session)
    for event in events:
        event.status = NotificationStatus.DEAD_LETTER.value
        event.last_error = "unknown_delivery_expired"
        if _is_inbound_response(
            ClaimedNotification(
                event_id=event.id,
                business_id=event.business_id,
                event_type=event.event_type,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                recipient_type=event.recipient_type,
                recipient_phone=event.recipient_phone,
                channel=event.channel,
                payload=event.payload,
                attempts=event.attempts,
                max_attempts=event.max_attempts,
            )
        ):
            await inbound_repo.mark_response_failed(event.business_id, event.entity_id)


async def _claim_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> ClaimedNotification | None:
    async with session_factory() as session:
        await _expire_unknown(session)
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
            recipient_type=item.recipient_type,
            recipient_phone=item.recipient_phone,
            channel=item.channel,
            payload=item.payload,
            attempts=item.attempts,
            max_attempts=item.max_attempts,
            claim_token=item.claim_token,
            claim_version=item.claim_version,
        )
        await session.commit()
        return claimed


async def _release_notification_claim(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedNotification,
) -> None:
    try:
        async with session_factory() as session:
            from fonely.repositories.inbound_events import _next_attempt_at

            await NotificationRepository(session).mark_failed(
                claimed.event_id,
                "shutdown_release",
                _next_attempt_at(claimed.attempts + 1),
            )
            await session.commit()
        logger.info(
            "notification_claim_released_on_shutdown",
            extra={"event_id": claimed.event_id},
        )
    except Exception:
        logger.warning(
            "notification_claim_release_failed",
            exc_info=True,
            extra={"event_id": claimed.event_id},
        )


def _snapshot_to_model(claimed: ClaimedNotification) -> NotificationOutboxEvent:
    event = NotificationOutboxEvent(
        id=claimed.event_id,
        business_id=claimed.business_id,
        event_type=claimed.event_type,
        entity_type=claimed.entity_type,
        entity_id=claimed.entity_id,
        recipient_type=claimed.recipient_type,
        recipient_phone=claimed.recipient_phone,
        channel=claimed.channel,
        payload=claimed.payload,
        status=NotificationStatus.PROCESSING.value,
        attempts=claimed.attempts,
        max_attempts=claimed.max_attempts,
        idempotency_key=f"snapshot-{claimed.event_id}",
    )
    return event


async def _require_claim(
    session: AsyncSession, claimed: ClaimedNotification
) -> NotificationOutboxEvent:
    if claimed.claim_token is None:
        raise RuntimeError("notification_claim_token_missing")
    event = await session.scalar(
        select(NotificationOutboxEvent).where(
            NotificationOutboxEvent.business_id == claimed.business_id,
            NotificationOutboxEvent.id == claimed.event_id,
            NotificationOutboxEvent.status == NotificationStatus.PROCESSING.value,
            NotificationOutboxEvent.claim_token == claimed.claim_token,
            NotificationOutboxEvent.claim_version == claimed.claim_version,
            NotificationOutboxEvent.lease_expires_at > datetime.now(UTC),
        )
    )
    if event is None:
        raise RuntimeError("notification_claim_stale")
    return event


async def _record_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedNotification,
    attempt_number: int,
    status: str,
) -> bool:
    async with session_factory() as session:
        await _require_claim(session, claimed)
        inserted = await session.scalar(
            pg_insert(WhatsAppDeliveryAttempt)
            .values(
                business_id=claimed.business_id,
                notification_event_id=claimed.event_id,
                attempt_number=attempt_number,
                status=status,
            )
            .on_conflict_do_nothing(constraint="uq_whatsapp_delivery_attempt")
            .returning(WhatsAppDeliveryAttempt.id)
        )
        await session.commit()
        return inserted is not None


async def _record_abandoned_sending(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedNotification,
    attempt_number: int,
) -> None:
    async with session_factory() as session:
        await _require_claim(session, claimed)
        now = datetime.now(UTC)
        await session.execute(
            update(WhatsAppDeliveryAttempt)
            .where(
                WhatsAppDeliveryAttempt.business_id == claimed.business_id,
                WhatsAppDeliveryAttempt.notification_event_id == claimed.event_id,
                WhatsAppDeliveryAttempt.attempt_number == attempt_number,
            )
            .values(
                status="unknown",
                error_class="worker_crash_after_send_possible",
                updated_at=now,
            )
        )
        await session.execute(
            update(NotificationOutboxEvent)
            .where(
                NotificationOutboxEvent.business_id == claimed.business_id,
                NotificationOutboxEvent.id == claimed.event_id,
                NotificationOutboxEvent.status == NotificationStatus.PROCESSING.value,
                NotificationOutboxEvent.claim_token == claimed.claim_token,
                NotificationOutboxEvent.claim_version == claimed.claim_version,
            )
            .values(
                status=NotificationStatus.UNKNOWN.value,
                last_error="worker_crash_after_send_possible",
                next_attempt_at=None,
                claim_token=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        await session.commit()


async def _record_accepted(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedNotification,
    attempt_number: int,
    receipt: DeliveryReceipt,
) -> None:
    async with session_factory() as session:
        await _require_claim(session, claimed)
        attempt_status = (
            "delivered"
            if receipt.final
            else ("accepted" if receipt.provider_message_id else "unknown")
        )
        await session.execute(
            update(WhatsAppDeliveryAttempt)
            .where(
                WhatsAppDeliveryAttempt.business_id == claimed.business_id,
                WhatsAppDeliveryAttempt.notification_event_id == claimed.event_id,
                WhatsAppDeliveryAttempt.attempt_number == attempt_number,
            )
            .values(
                status=attempt_status,
                provider_message_id=receipt.provider_message_id,
                error_class=(
                    None if receipt.provider_message_id else "missing_provider_message_id"
                ),
                updated_at=datetime.now(UTC),
            )
        )
        now = datetime.now(UTC)
        if receipt.final:
            await session.execute(
                update(NotificationOutboxEvent)
                .where(
                    NotificationOutboxEvent.business_id == claimed.business_id,
                    NotificationOutboxEvent.id == claimed.event_id,
                    NotificationOutboxEvent.status == NotificationStatus.PROCESSING.value,
                    NotificationOutboxEvent.claim_token == claimed.claim_token,
                    NotificationOutboxEvent.claim_version == claimed.claim_version,
                )
                .values(
                    status=NotificationStatus.DELIVERED.value,
                    attempts=attempt_number,
                    delivered_at=now,
                    last_error=None,
                    next_attempt_at=None,
                    claim_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            if _is_inbound_response(claimed):
                inbound_repo = InboundEventRepository(session)
                payload = claimed.payload if isinstance(claimed.payload, dict) else {}
                if payload.get("terminal_fallback") is True:
                    await inbound_repo.mark_fallback_completed(
                        claimed.business_id, claimed.entity_id, now
                    )
                else:
                    await inbound_repo.mark_completed(claimed.business_id, claimed.entity_id, now)
        else:
            await session.execute(
                update(NotificationOutboxEvent)
                .where(
                    NotificationOutboxEvent.business_id == claimed.business_id,
                    NotificationOutboxEvent.id == claimed.event_id,
                    NotificationOutboxEvent.status == NotificationStatus.PROCESSING.value,
                    NotificationOutboxEvent.claim_token == claimed.claim_token,
                    NotificationOutboxEvent.claim_version == claimed.claim_version,
                )
                .values(
                    status=NotificationStatus.UNKNOWN.value,
                    attempts=attempt_number,
                    last_error="awaiting_provider_status",
                    next_attempt_at=None,
                    claim_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
        await session.commit()


async def _record_send_failure(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedNotification,
    attempt_number: int,
    exc: NotificationDeliveryError,
) -> None:
    async with session_factory() as session:
        await _require_claim(session, claimed)
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
                NotificationOutboxEvent.claim_token == claimed.claim_token,
                NotificationOutboxEvent.claim_version == claimed.claim_version,
            )
            .values(
                status=outbox_status,
                attempts=attempt_number,
                last_error=exc.error[:500],
                next_attempt_at=next_at,
                claim_token=None,
                lease_expires_at=None,
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
