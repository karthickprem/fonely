"""WhatsApp webhook handler — thin adapter to ConversationService."""

import hashlib
import hmac
import json
import logging
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.api.internal.validation import InternalValidationPort
from fonely.core.config import settings
from fonely.domain.conversation.state import ConversationState
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.services.appointments import AppointmentService
from fonely.services.conversation import ConversationService, find_or_create_conversation_persistent
from fonely.services.whatsapp_config import WhatsAppBusinessMapping
from fonely.services.whatsapp_sender import WhatsAppSender

logger = logging.getLogger("fonely.api.channels.whatsapp")

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])

_PROCESSED_MESSAGE_IDS: OrderedDict[str, None] = OrderedDict()
_MAX_PROCESSED_IDS = 10000


def _verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _is_duplicate(message_id: str) -> bool:
    if message_id in _PROCESSED_MESSAGE_IDS:
        return True
    _PROCESSED_MESSAGE_IDS[message_id] = None
    if len(_PROCESSED_MESSAGE_IDS) > _MAX_PROCESSED_IDS:
        _PROCESSED_MESSAGE_IDS.popitem(last=False)
    return False


@router.get("")
async def verify_webhook(request: Request) -> Response:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")

    if mode == "subscribe" and hmac.compare_digest(token or "", settings.whatsapp_verify_token):
        return Response(content=challenge, media_type="text/plain")

    return Response(status_code=403)


@router.post("")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    try:
        body = await request.body()
        if settings.whatsapp_app_secret:
            sig = request.headers.get("X-Hub-Signature-256", "")
            if not _verify_webhook_signature(body, sig, settings.whatsapp_app_secret):
                logger.warning("whatsapp_invalid_signature")
                return Response(status_code=200)
        payload = json.loads(body)
    except Exception:
        return Response(status_code=200)

    background_tasks.add_task(_process_webhook, payload, request.app)
    return Response(status_code=200)


async def _process_webhook(payload: dict, app: object) -> None:  # type: ignore[type-arg]
    try:
        entries = payload.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id", "")

                for message in messages:
                    await _handle_message(message, phone_number_id, app)
    except Exception:
        logger.warning("whatsapp_webhook_processing_error")


async def _handle_message(
    message: dict[str, object],
    phone_number_id: str,
    app: object,
) -> None:
    message_id = str(message.get("id", ""))
    message_type = str(message.get("type", ""))
    sender_phone = str(message.get("from", ""))

    if not sender_phone or not message_id:
        return

    if _is_duplicate(message_id):
        logger.info(
            "whatsapp_duplicate_message",
            extra={"message_id": message_id},
        )
        return

    sender = _get_sender(app)
    if sender is None:
        return

    if message_type != "text":
        await sender.send_text(
            sender_phone,
            "I can currently help with text messages. Please type your request.",
        )
        return

    text_body = ""
    text_obj = message.get("text")
    if isinstance(text_obj, dict):
        text_body = str(text_obj.get("body", ""))
    if not text_body:
        return

    mapping = WhatsAppBusinessMapping()
    business_id = mapping.get_business_id(phone_number_id)
    if business_id is None:
        logger.warning(
            "whatsapp_unknown_phone_number_id",
            extra={"phone_number_id": phone_number_id},
        )
        return

    from fonely.core.pii_audit import log_pii_access

    log_pii_access(
        operation="read",
        data_type="conversation",
        business_id=business_id,
        accessor="api:whatsapp",
        record_count=1,
    )

    phone_formatted = f"+{sender_phone}" if not sender_phone.startswith("+") else sender_phone

    factory = getattr(app, "state", None)
    if factory is None:
        return
    session_factory: async_sessionmaker[AsyncSession] = getattr(factory, "session_factory", None)  # type: ignore[assignment]
    gateway = getattr(factory, "model_gateway", None)
    if session_factory is None or gateway is None:
        return

    if await _is_duplicate_persistent(message_id, business_id, session_factory):
        logger.info("whatsapp_duplicate_message_db", extra={"message_id": message_id})
        return

    async with session_factory() as session:
        try:
            if await _is_owner(business_id, phone_formatted, session):
                from fonely.services.owner_commands import OwnerCommandService

                owner_svc = OwnerCommandService(session, gateway)
                result = await owner_svc.process_command(business_id, phone_formatted, text_body)
                await session.commit()
                await sender.send_text(sender_phone, result.response_text)
                return

            ctx = await find_or_create_conversation_persistent(
                business_id, phone_formatted, session
            )
            actor = ActorContext(
                business_id=business_id,
                normalized_phone=phone_formatted,
                verified_role=CallerRole.CUSTOMER,
                session_id=None,
            )
            validation = InternalValidationPort(session)
            appt_service = AppointmentService(session, validation=validation)
            conv_service = ConversationService(session, gateway, appointment_service=appt_service)
            turn = await conv_service.process_message(
                ctx.conversation_id,
                business_id,
                actor,
                text_body,
            )
            if ctx.state in (ConversationState.COMPLETED, ConversationState.ENDED):
                from fonely.services.conversation_persistence import (
                    ConversationPersistenceService,
                )

                persistence = ConversationPersistenceService(session)
                await persistence.mark_completed(ctx.conversation_id)

            await session.commit()
            await sender.send_text(sender_phone, turn.assistant_response)
        except Exception:
            logger.warning(
                "whatsapp_message_processing_error",
                extra={"business_id": business_id, "phone_suffix": sender_phone[-4:]},
            )
            await sender.send_text(
                sender_phone,
                "Sorry, something went wrong. Please try again.",
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


async def _is_duplicate_persistent(
    message_id: str,
    business_id: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    from sqlalchemy import text

    try:
        async with session_factory() as session:
            result = await session.execute(
                text(
                    "INSERT INTO whatsapp_processed_messages "
                    "(message_id, business_id) VALUES (:mid, :bid) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"mid": message_id, "bid": business_id},
            )
            await session.commit()
            return getattr(result, "rowcount", 0) == 0
    except Exception:
        return False


def _get_sender(app: object) -> WhatsAppSender | None:
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return None
    client = getattr(getattr(app, "state", None), "http_client", None)
    return WhatsAppSender(
        access_token=settings.whatsapp_access_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        client=client,
    )
