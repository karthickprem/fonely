"""Durable inbound WhatsApp message worker.

Polls whatsapp_inbound_events for received/failed events, processes
them through the conversation pipeline, and marks them completed.
Failed events are retried with exponential backoff.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.api.internal.validation import InternalValidationPort
from fonely.domain.conversation.state import ConversationState
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.models.schema import WhatsAppInboundEvent
from fonely.repositories.inbound_events import InboundEventRepository
from fonely.services.appointments import AppointmentService
from fonely.services.conversation import ConversationService, find_or_create_conversation_persistent
from fonely.services.whatsapp_sender import WhatsAppSender

logger = logging.getLogger("fonely.workers.inbound")

BACKOFF_SECONDS = (30, 60, 120, 300, 600)


def _next_attempt_at(attempts: int) -> datetime:
    index = min(attempts, len(BACKOFF_SECONDS) - 1)
    return datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[index])


async def run_inbound_worker(
    session_factory: async_sessionmaker[AsyncSession],
    model_gateway: object,
    sender: WhatsAppSender | None,
    *,
    poll_interval: float = 2.0,
    batch_size: int = 10,
    max_iterations: int | None = None,
) -> None:
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        async with session_factory() as session:
            repo = InboundEventRepository(session)
            events = await repo.claim_pending_events(limit=batch_size)

            for event in events:
                try:
                    await _process_event(event, session, model_gateway, sender)
                    await repo.mark_completed(event.id, datetime.now(UTC))
                except Exception as exc:
                    next_at = _next_attempt_at(event.attempts)
                    await repo.mark_failed(event.id, type(exc).__name__, next_at)
                    logger.warning(
                        "inbound_event_processing_failed",
                        extra={
                            "event_id": event.id,
                            "message_id": event.message_id,
                            "error": type(exc).__name__,
                            "attempt": event.attempts,
                        },
                    )

            await session.commit()

        if max_iterations is None:
            await asyncio.sleep(poll_interval)


async def _process_event(
    event: WhatsAppInboundEvent,
    session: AsyncSession,
    model_gateway: object,
    sender: WhatsAppSender | None,
) -> None:
    phone = event.sender_phone
    phone_formatted = f"+{phone}" if not phone.startswith("+") else phone
    business_id = event.business_id

    from fonely.core.pii_audit import log_pii_access

    log_pii_access(
        operation="read",
        data_type="conversation",
        business_id=business_id,
        accessor="worker:inbound",
        record_count=1,
    )

    if event.message_type != "text":
        if sender:
            await sender.send_text(
                phone,
                "I can currently help with text messages. Please type your request.",
            )
        return

    text_body = event.message_body or ""
    if not text_body:
        return

    if await _is_owner(business_id, phone_formatted, session):
        from fonely.services.owner_commands import OwnerCommandService

        owner_svc = OwnerCommandService(session, model_gateway)
        result = await owner_svc.process_command(business_id, phone_formatted, text_body)
        if sender:
            await sender.send_text(phone, result.response_text)
        return

    ctx = await find_or_create_conversation_persistent(business_id, phone_formatted, session)
    actor = ActorContext(
        business_id=business_id,
        normalized_phone=phone_formatted,
        verified_role=CallerRole.CUSTOMER,
        session_id=None,
    )
    validation = InternalValidationPort(session)
    appt_service = AppointmentService(session, validation=validation)
    conv_service = ConversationService(session, model_gateway, appointment_service=appt_service)
    turn = await conv_service.process_message(
        ctx.conversation_id,
        business_id,
        actor,
        text_body,
    )

    if ctx.state in (ConversationState.COMPLETED, ConversationState.ENDED):
        from fonely.services.conversation_persistence import ConversationPersistenceService

        persistence = ConversationPersistenceService(session)
        await persistence.mark_completed(ctx.conversation_id)

    if sender:
        await sender.send_text(phone, turn.assistant_response)


async def _is_owner(business_id: int, phone: str, session: AsyncSession) -> bool:
    try:
        from fonely.models.enums import BusinessUserRole
        from fonely.models.schema import BusinessUser

        owner = await session.scalar(
            select(BusinessUser).where(
                BusinessUser.business_id == business_id,
                BusinessUser.phone == phone,
                BusinessUser.role == BusinessUserRole.OWNER.value,
                BusinessUser.is_active.is_(True),
            )
        )
        return isinstance(owner, BusinessUser)
    except Exception:
        return False
