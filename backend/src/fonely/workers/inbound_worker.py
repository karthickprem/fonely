"""Durable inbound WhatsApp message worker.

Claims one event at a time, processes domain logic, enqueues outbound
response via notification_outbox, and commits. Advisory locks enforce
per-conversation ordering. Failed events are retried with backoff.
The notification worker handles actual WhatsApp delivery.
"""

import asyncio
import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.api.internal.validation import InternalValidationPort
from fonely.domain.conversation.state import ConversationState
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import (
    CallerRole,
    NotificationChannel,
    NotificationEventType,
    NotificationRecipientType,
)
from fonely.models.schema import WhatsAppInboundEvent
from fonely.repositories.inbound_events import InboundEventRepository
from fonely.repositories.notifications import NotificationRepository
from fonely.services.appointments import AppointmentService
from fonely.services.conversation import (
    ConversationService,
    find_or_create_conversation_persistent,
)
from fonely.services.model_gateway import ModelGateway

logger = logging.getLogger("fonely.workers.inbound")


async def run_inbound_worker(
    session_factory: async_sessionmaker[AsyncSession],
    model_gateway: ModelGateway,
    *,
    poll_interval: float = 2.0,
    max_iterations: int | None = None,
) -> None:
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1

        async with session_factory() as session:
            repo = InboundEventRepository(session)
            events = await repo.claim_pending_events(limit=1)
            if not events:
                await session.commit()
                if max_iterations is None:
                    await asyncio.sleep(poll_interval)
                continue

            event = events[0]

            try:
                lock_key = hash((event.business_id, event.sender_phone)) & 0x7FFFFFFFFFFFFFFF
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

                response_text = await _process_domain(event, session, model_gateway)

                await _enqueue_outbound_response(event, response_text, session)
                await repo.mark_domain_processed(event.business_id, event.id)
                await session.commit()

            except Exception as exc:
                await session.rollback()
                async with session_factory() as fail_session:
                    fail_repo = InboundEventRepository(fail_session)
                    await fail_repo.mark_failed(event.business_id, event.id, type(exc).__name__)
                    await fail_session.commit()
                logger.warning(
                    "inbound_event_processing_failed",
                    extra={
                        "event_id": event.id,
                        "message_id": event.message_id,
                        "error": type(exc).__name__,
                        "attempt": event.attempts,
                    },
                )


async def _process_domain(
    event: WhatsAppInboundEvent,
    session: AsyncSession,
    model_gateway: ModelGateway,
) -> str:
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
        return "I can currently help with text messages. Please type your request."

    text_body = event.message_body or ""
    if not text_body:
        return "I didn't receive a message. Please try again."

    if await _is_owner(business_id, phone_formatted, session):
        from fonely.services.owner_commands import OwnerCommandService

        owner_svc = OwnerCommandService(session, model_gateway)
        result = await owner_svc.process_command(business_id, phone_formatted, text_body)
        return result.response_text

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

    return turn.assistant_response


async def _enqueue_outbound_response(
    event: WhatsAppInboundEvent,
    response_text: str,
    session: AsyncSession,
) -> None:
    repo = NotificationRepository(session)
    await repo.insert_event_idempotent(
        {
            "business_id": event.business_id,
            "event_type": NotificationEventType.WHATSAPP_INBOUND_RESPONSE.value,
            "entity_type": "whatsapp_inbound_event",
            "entity_id": event.id,
            "recipient_type": NotificationRecipientType.PATIENT.value,
            "recipient_phone": event.sender_phone,
            "channel": NotificationChannel.WHATSAPP.value,
            "payload": {
                "response_text": response_text,
                "phone_number_id": event.phone_number_id,
            },
            "idempotency_key": f"whatsapp-response-{event.message_id}",
        }
    )


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
