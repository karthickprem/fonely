"""Durable inbound WhatsApp worker with lock-free provider calls.

Phase A claims one ordered event in a short transaction and commits the lease.
Phase B/C repeatedly dry-runs domain processing with a deferred model gateway.
When a model response is needed, the transaction rolls back and the real provider
is called without a database session or lock. The domain transaction is replayed
with the durable committed state plus recorded model responses, then commits the
mutation, conversation turn, outboxes and inbound state atomically.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
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
from fonely.services.model_gateway import ModelGateway, ModelResponse

logger = logging.getLogger("fonely.workers.inbound")

_FALLBACK_RESPONSE = "Sorry, I couldn't process that request. Please call the clinic or try again."


@dataclass(frozen=True)
class ProviderRequest:
    system_prompt: str
    messages: list[dict[str, str]]
    tools: list[dict[str, object]] | None
    temperature: float
    max_tokens: int


class ProviderCallRequiredError(Exception):
    def __init__(self, request: ProviderRequest) -> None:
        super().__init__("provider response required")
        self.request = request


class DeferredModelGateway:
    """Replay only responses whose provider request exactly matches."""

    def __init__(self, exchanges: list[tuple[ProviderRequest, ModelResponse]]) -> None:
        self._exchanges = exchanges
        self._index = 0

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, object]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 500,
    ) -> ModelResponse:
        request = ProviderRequest(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if self._index < len(self._exchanges):
            expected, response = self._exchanges[self._index]
            if expected != request:
                raise RuntimeError("provider_request_changed_during_replay")
            self._index += 1
            return response
        raise ProviderCallRequiredError(request)


@dataclass(frozen=True)
class ClaimedEvent:
    event_id: int
    business_id: int
    message_id: str
    sender_phone: str
    message_type: str
    message_body: str | None
    phone_number_id: str
    claim_token: uuid.UUID
    claim_version: int
    attempts: int
    max_attempts: int


async def run_inbound_worker(
    session_factory: async_sessionmaker[AsyncSession],
    model_gateway: ModelGateway,
    *,
    poll_interval: float = 2.0,
    max_iterations: int | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    iterations = 0
    consecutive_failures = 0

    def _should_continue() -> bool:
        if stop is not None and stop.is_set():
            return False
        return not (max_iterations is not None and iterations >= max_iterations)

    async def _interruptible_sleep(seconds: float) -> None:
        if stop is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=seconds)
        else:
            await asyncio.sleep(seconds)

    while _should_continue():
        iterations += 1
        try:
            claimed = await _claim(session_factory)
            if claimed is None:
                consecutive_failures = 0
                if max_iterations is None:
                    await _interruptible_sleep(poll_interval)
                continue

            if stop is not None and stop.is_set():
                await _release_claim(session_factory, claimed)
                break

            await _process_claimed(session_factory, claimed, model_gateway)
            consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.error(
                "inbound_poll_iteration_failed",
                exc_info=True,
                extra={"consecutive_failures": consecutive_failures},
            )
            if max_iterations is None:
                await _interruptible_sleep(min(30.0, 2.0**consecutive_failures))


async def _claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> ClaimedEvent | None:
    async with session_factory() as session:
        event = await InboundEventRepository(session).claim_next_eligible()
        if event is None:
            await session.commit()
            return None
        if event.claim_token is None:
            raise RuntimeError("claim_token_missing_after_claim")
        claimed = ClaimedEvent(
            event_id=event.id,
            business_id=event.business_id,
            message_id=event.message_id,
            sender_phone=event.sender_phone,
            message_type=event.message_type,
            message_body=event.message_body,
            phone_number_id=event.phone_number_id,
            claim_token=event.claim_token,
            claim_version=event.claim_version,
            attempts=event.attempts,
            max_attempts=event.max_attempts,
        )
        await session.commit()
        return claimed


async def _process_claimed(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedEvent,
    provider: ModelGateway,
) -> None:
    exchanges: list[tuple[ProviderRequest, ModelResponse]] = []

    while True:
        deferred = DeferredModelGateway(exchanges)
        try:
            await _commit_attempt(session_factory, claimed, deferred)
            return
        except ProviderCallRequiredError as needed:
            invalidate_conversation_cache(claimed.business_id, _normalized_phone(claimed))
            try:
                response = await provider.complete(
                    system_prompt=needed.request.system_prompt,
                    messages=needed.request.messages,
                    tools=needed.request.tools,
                    temperature=needed.request.temperature,
                    max_tokens=needed.request.max_tokens,
                )
            except Exception as exc:
                await _record_failure(session_factory, claimed, exc)
                return
            exchanges.append((needed.request, response))
        except Exception as exc:
            invalidate_conversation_cache(claimed.business_id, _normalized_phone(claimed))
            await _record_failure(session_factory, claimed, exc)
            return


async def _commit_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedEvent,
    gateway: ModelGateway,
) -> None:
    async with session_factory() as session:
        repo = InboundEventRepository(session)
        await repo.acquire_conversation_lock(claimed.business_id, _normalized_phone(claimed))
        await repo.require_owned_claim(
            claimed.business_id,
            claimed.event_id,
            claimed.claim_token,
            claimed.claim_version,
        )

        response_text, recipient_type = await _process_domain(claimed, session, gateway)
        await _enqueue_response(
            claimed,
            response_text,
            session,
            recipient_type=recipient_type,
        )
        await repo.verify_and_mark_domain_processed(
            claimed.business_id,
            claimed.event_id,
            claimed.claim_token,
            claimed.claim_version,
        )
        await session.commit()


def _normalized_phone(claimed: ClaimedEvent) -> str:
    return (
        claimed.sender_phone if claimed.sender_phone.startswith("+") else f"+{claimed.sender_phone}"
    )


async def _process_domain(
    claimed: ClaimedEvent,
    session: AsyncSession,
    gateway: ModelGateway,
) -> tuple[str, str]:
    if claimed.message_type != "text":
        return (
            "I can currently help with text messages. Please type your request.",
            NotificationRecipientType.PATIENT.value,
        )
    if not claimed.message_body:
        return (
            "I didn't receive a message. Please try again.",
            NotificationRecipientType.PATIENT.value,
        )

    phone = _normalized_phone(claimed)
    if await _is_owner(claimed.business_id, phone, session):
        from fonely.services.owner_commands import OwnerCommandService

        result = await OwnerCommandService(session, gateway).process_command(
            claimed.business_id, phone, claimed.message_body
        )
        return result.response_text, NotificationRecipientType.OWNER.value

    ctx = await find_or_create_conversation_persistent(claimed.business_id, phone, session)
    actor = ActorContext(
        business_id=claimed.business_id,
        normalized_phone=phone,
        verified_role=CallerRole.CUSTOMER,
        session_id=None,
    )
    validation = InternalValidationPort(session)
    appointment_service = AppointmentService(session, validation=validation)
    turn = await ConversationService(
        session, gateway, appointment_service=appointment_service
    ).process_message(
        ctx.conversation_id,
        claimed.business_id,
        actor,
        claimed.message_body,
    )

    if ctx.state in (ConversationState.COMPLETED, ConversationState.ENDED):
        from fonely.services.conversation_persistence import ConversationPersistenceService

        await ConversationPersistenceService(session).mark_completed(ctx.conversation_id)
    return turn.assistant_response, NotificationRecipientType.PATIENT.value


async def _enqueue_response(
    claimed: ClaimedEvent,
    response_text: str,
    session: AsyncSession,
    *,
    terminal_fallback: bool = False,
    recipient_type: str = NotificationRecipientType.PATIENT.value,
) -> None:
    await NotificationRepository(session).insert_event_idempotent(
        {
            "business_id": claimed.business_id,
            "event_type": NotificationEventType.WHATSAPP_INBOUND_RESPONSE.value,
            "entity_type": "whatsapp_inbound_event",
            "entity_id": claimed.event_id,
            "recipient_type": recipient_type,
            "recipient_phone": claimed.sender_phone,
            "channel": NotificationChannel.WHATSAPP.value,
            "payload": {
                "response_text": response_text,
                "phone_number_id": claimed.phone_number_id,
                "claim_token": str(claimed.claim_token),
                "terminal_fallback": terminal_fallback,
            },
            "idempotency_key": f"whatsapp-response-{claimed.message_id}",
        }
    )


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedEvent,
    exc: Exception,
) -> None:
    terminal = claimed.attempts + 1 >= claimed.max_attempts
    try:
        async with session_factory() as session:
            repo = InboundEventRepository(session)
            changed = await repo.mark_failed(
                claimed.business_id,
                claimed.event_id,
                claimed.claim_token,
                claimed.claim_version,
                type(exc).__name__,
            )
            if changed and terminal:
                await _enqueue_response(
                    claimed,
                    _FALLBACK_RESPONSE,
                    session,
                    terminal_fallback=True,
                )
            await session.commit()
    except Exception:
        logger.error(
            "inbound_failure_bookkeeping_failed",
            exc_info=True,
            extra={"event_id": claimed.event_id},
        )
        raise

    logger.warning(
        "inbound_event_processing_failed",
        extra={
            "event_id": claimed.event_id,
            "message_id": claimed.message_id,
            "error": type(exc).__name__,
            "attempt": claimed.attempts + 1,
            "terminal": terminal,
        },
    )


async def _release_claim(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedEvent,
) -> None:
    try:
        async with session_factory() as session:
            repo = InboundEventRepository(session)
            await repo.mark_failed(
                claimed.business_id,
                claimed.event_id,
                claimed.claim_token,
                claimed.claim_version,
                "shutdown_release",
            )
            await session.commit()
        logger.info(
            "inbound_claim_released_on_shutdown",
            extra={"event_id": claimed.event_id},
        )
    except Exception:
        logger.warning(
            "inbound_claim_release_failed",
            exc_info=True,
            extra={"event_id": claimed.event_id},
        )


async def _is_owner(business_id: int, phone: str, session: AsyncSession) -> bool:
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
