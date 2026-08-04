"""Durable inbound WhatsApp message worker — 3-phase architecture.

Phase A: Short claim transaction — claim oldest eligible event, commit, release locks.
Phase B: Provider/reasoning — call LLM with no DB locks held.
Phase C: Short commit transaction — advisory lock, verify claim, apply domain
         mutation, enqueue outbound response, mark domain_processed, commit atomically.

Failed events are retried with exponential backoff. The notification worker
handles actual WhatsApp delivery and marks the inbound event completed.
"""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
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
from fonely.repositories.inbound_events import InboundEventRepository
from fonely.repositories.notifications import NotificationRepository
from fonely.services.appointments import AppointmentService
from fonely.services.conversation import (
    ConversationService,
    find_or_create_conversation_persistent,
    invalidate_conversation_cache,
)
from fonely.services.model_gateway import ModelGateway

logger = logging.getLogger("fonely.workers.inbound")

_FALLBACK_RESPONSE = "Sorry, I couldn't process that request. Please call the clinic or try again."


@dataclass
class ClaimedEvent:
    """Immutable snapshot of claimed event scalars — safe to use after rollback."""

    event_id: int
    business_id: int
    message_id: str
    sender_phone: str
    message_type: str
    message_body: str | None
    phone_number_id: str | None
    claim_token: object
    attempts: int
    max_attempts: int


async def run_inbound_worker(
    session_factory: async_sessionmaker[AsyncSession],
    model_gateway: ModelGateway,
    *,
    poll_interval: float = 2.0,
    max_iterations: int | None = None,
) -> None:
    iterations = 0
    consecutive_failures = 0

    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            claimed = await _phase_a_claim(session_factory)
            if claimed is None:
                consecutive_failures = 0
                if max_iterations is None:
                    await asyncio.sleep(poll_interval)
                continue

            try:
                response_text = await _phase_b_reason(claimed, session_factory, model_gateway)
            except Exception as exc:
                await _handle_failure(session_factory, claimed, exc)
                continue

            try:
                await _phase_c_commit(session_factory, claimed, response_text, model_gateway)
            except Exception as exc:
                await _handle_failure(session_factory, claimed, exc)
                continue

            consecutive_failures = 0

        except Exception:
            consecutive_failures += 1
            backoff = min(30.0, 2.0**consecutive_failures)
            logger.error(
                "inbound_poll_iteration_failed",
                exc_info=True,
                extra={"consecutive_failures": consecutive_failures},
            )
            if max_iterations is None:
                await asyncio.sleep(backoff)


async def _phase_a_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> ClaimedEvent | None:
    """Short transaction: claim the oldest eligible event, commit immediately."""
    async with session_factory() as session:
        repo = InboundEventRepository(session)
        event = await repo.claim_next_eligible()
        if event is None:
            await session.commit()
            return None
        claimed = ClaimedEvent(
            event_id=event.id,
            business_id=event.business_id,
            message_id=event.message_id,
            sender_phone=event.sender_phone,
            message_type=event.message_type,
            message_body=event.message_body,
            phone_number_id=event.phone_number_id,
            claim_token=event.claim_token,
            attempts=event.attempts,
            max_attempts=event.max_attempts,
        )
        await session.commit()
    return claimed


async def _phase_b_reason(
    claimed: ClaimedEvent,
    session_factory: async_sessionmaker[AsyncSession],
    model_gateway: ModelGateway,
) -> str:
    """Provider/reasoning phase — no DB locks held during LLM calls."""
    if claimed.message_type != "text":
        return "I can currently help with text messages. Please type your request."

    text_body = claimed.message_body or ""
    if not text_body:
        return "I didn't receive a message. Please try again."

    phone_formatted = (
        f"+{claimed.sender_phone}"
        if not claimed.sender_phone.startswith("+")
        else claimed.sender_phone
    )

    async with session_factory() as session:
        is_owner = await _is_owner(claimed.business_id, phone_formatted, session)

    if is_owner:
        async with session_factory() as session:
            from fonely.services.owner_commands import OwnerCommandService

            owner_svc = OwnerCommandService(session, model_gateway)
            result = await owner_svc.process_command(
                claimed.business_id, phone_formatted, text_body
            )
            await session.commit()
        return result.response_text

    return text_body


async def _phase_c_commit(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedEvent,
    response_text: str,
    model_gateway: ModelGateway,
) -> None:
    """Short commit transaction: advisory lock, verify claim, apply domain, enqueue response."""
    phone_formatted = (
        f"+{claimed.sender_phone}"
        if not claimed.sender_phone.startswith("+")
        else claimed.sender_phone
    )

    async with session_factory() as session:
        repo = InboundEventRepository(session)
        await repo.acquire_conversation_lock(claimed.business_id, claimed.sender_phone)

        if claimed.message_type == "text" and claimed.message_body:
            from fonely.core.pii_audit import log_pii_access

            log_pii_access(
                operation="read",
                data_type="conversation",
                business_id=claimed.business_id,
                accessor="worker:inbound",
                record_count=1,
            )

            if not await _is_owner(claimed.business_id, phone_formatted, session):
                ctx = await find_or_create_conversation_persistent(
                    claimed.business_id, phone_formatted, session
                )
                actor = ActorContext(
                    business_id=claimed.business_id,
                    normalized_phone=phone_formatted,
                    verified_role=CallerRole.CUSTOMER,
                    session_id=None,
                )
                validation = InternalValidationPort(session)
                appt_service = AppointmentService(session, validation=validation)
                conv_service = ConversationService(
                    session, model_gateway, appointment_service=appt_service
                )
                turn = await conv_service.process_message(
                    ctx.conversation_id,
                    claimed.business_id,
                    actor,
                    response_text,
                )
                response_text = turn.assistant_response

                if ctx.state in (ConversationState.COMPLETED, ConversationState.ENDED):
                    from fonely.services.conversation_persistence import (
                        ConversationPersistenceService,
                    )

                    persistence = ConversationPersistenceService(session)
                    await persistence.mark_completed(ctx.conversation_id)

        await _enqueue_outbound_response(claimed, response_text, session)
        await repo.verify_and_mark_domain_processed(
            claimed.business_id, claimed.event_id, claimed.claim_token
        )
        await session.commit()


async def _handle_failure(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedEvent,
    exc: Exception,
) -> None:
    """Mark event failed using captured scalars — no ORM state dependency after rollback."""
    invalidate_conversation_cache(claimed.business_id, claimed.sender_phone)

    error_name = type(exc).__name__
    is_terminal = claimed.attempts + 1 >= claimed.max_attempts

    try:
        async with session_factory() as fail_session:
            fail_repo = InboundEventRepository(fail_session)
            await fail_repo.mark_failed(claimed.business_id, claimed.event_id, error_name)

            if is_terminal:
                notif_repo = NotificationRepository(fail_session)
                await notif_repo.insert_event_idempotent(
                    {
                        "business_id": claimed.business_id,
                        "event_type": NotificationEventType.WHATSAPP_INBOUND_RESPONSE.value,
                        "entity_type": "whatsapp_inbound_event",
                        "entity_id": claimed.event_id,
                        "recipient_type": NotificationRecipientType.PATIENT.value,
                        "recipient_phone": claimed.sender_phone,
                        "channel": NotificationChannel.WHATSAPP.value,
                        "payload": {
                            "response_text": _FALLBACK_RESPONSE,
                            "phone_number_id": claimed.phone_number_id,
                        },
                        "idempotency_key": f"whatsapp-response-{claimed.message_id}",
                    }
                )

            await fail_session.commit()
    except Exception:
        logger.error(
            "inbound_failure_bookkeeping_failed",
            exc_info=True,
            extra={"event_id": claimed.event_id},
        )

    logger.warning(
        "inbound_event_processing_failed",
        extra={
            "event_id": claimed.event_id,
            "message_id": claimed.message_id,
            "error": error_name,
            "attempt": claimed.attempts,
            "terminal": is_terminal,
        },
    )


async def _enqueue_outbound_response(
    claimed: ClaimedEvent,
    response_text: str,
    session: AsyncSession,
) -> None:
    repo = NotificationRepository(session)
    await repo.insert_event_idempotent(
        {
            "business_id": claimed.business_id,
            "event_type": NotificationEventType.WHATSAPP_INBOUND_RESPONSE.value,
            "entity_type": "whatsapp_inbound_event",
            "entity_id": claimed.event_id,
            "recipient_type": NotificationRecipientType.PATIENT.value,
            "recipient_phone": claimed.sender_phone,
            "channel": NotificationChannel.WHATSAPP.value,
            "payload": {
                "response_text": response_text,
                "phone_number_id": claimed.phone_number_id,
            },
            "idempotency_key": f"whatsapp-response-{claimed.message_id}",
        }
    )


async def _is_owner(business_id: int, phone: str, session: AsyncSession) -> bool:
    from fonely.models.enums import BusinessUserRole
    from fonely.models.schema import BusinessUser

    result = await session.scalar(
        select(BusinessUser).where(
            BusinessUser.business_id == business_id,
            BusinessUser.phone == phone,
            BusinessUser.role == BusinessUserRole.OWNER.value,
            BusinessUser.is_active.is_(True),
        )
    )
    return isinstance(result, BusinessUser)
