"""WhatsApp webhook handler — durable inbound event pattern.

Messages are persisted to the whatsapp_inbound_events table before
returning 200 to Meta. A separate inbound worker processes them with
retry. No message is permanently lost as long as the database is
available.

Signature verification is mandatory when the route is enabled.
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.config import settings
from fonely.services.whatsapp_config import WhatsAppBusinessMapping

logger = logging.getLogger("fonely.api.channels.whatsapp")

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])

_MAX_BODY_BYTES = 1_048_576
_MAX_ENTRIES = 10
_MAX_CHANGES = 10
_MAX_MESSAGES = 20


def _verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


@router.get("")
async def verify_webhook(request: Request) -> Response:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")

    if mode == "subscribe" and hmac.compare_digest(token or "", settings.whatsapp_verify_token):
        return Response(content=challenge, media_type="text/plain")

    return Response(status_code=403)


@router.post("")
async def handle_webhook(request: Request) -> Response:
    if not settings.whatsapp_app_secret:
        logger.error("whatsapp_webhook_secret_not_configured")
        return Response(status_code=503)

    try:
        body = await request.body()
        if len(body) > _MAX_BODY_BYTES:
            logger.warning("whatsapp_webhook_body_too_large", extra={"size": len(body)})
            return Response(status_code=200)

        sig = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_webhook_signature(body, sig, settings.whatsapp_app_secret):
            logger.warning("whatsapp_invalid_signature")
            return Response(status_code=200)

        payload = json.loads(body)
    except Exception:
        return Response(status_code=200)

    if not isinstance(payload, dict):
        return Response(status_code=200)

    factory = getattr(getattr(request.app, "state", None), "session_factory", None)
    if factory is None:
        logger.error("whatsapp_webhook_no_session_factory")
        return Response(status_code=503)

    mapping = WhatsAppBusinessMapping()
    persisted = 0

    try:
        async with factory() as session:
            entries = payload.get("entry", [])
            if not isinstance(entries, list):
                return Response(status_code=200)

            for entry in entries[:_MAX_ENTRIES]:
                if not isinstance(entry, dict):
                    continue
                changes = entry.get("changes", [])
                if not isinstance(changes, list):
                    continue

                for change in changes[:_MAX_CHANGES]:
                    if not isinstance(change, dict):
                        continue
                    value = change.get("value")
                    if not isinstance(value, dict):
                        continue
                    metadata = value.get("metadata")
                    if not isinstance(metadata, dict):
                        continue

                    phone_number_id = metadata.get("phone_number_id")
                    if not isinstance(phone_number_id, str) or not phone_number_id:
                        continue

                    business_id = mapping.get_business_id(phone_number_id)
                    if business_id is None:
                        continue

                    messages = value.get("messages")
                    if not isinstance(messages, list):
                        continue

                    for message in messages[:_MAX_MESSAGES]:
                        count = await _persist_inbound_event(
                            session, message, business_id, phone_number_id
                        )
                        persisted += count

            if persisted > 0:
                await session.commit()
    except Exception:
        logger.error("whatsapp_webhook_persistence_failed", exc_info=True)
        return Response(status_code=503)

    return Response(status_code=200)


async def _persist_inbound_event(
    session: AsyncSession,
    message: object,
    business_id: int,
    phone_number_id: str,
) -> int:
    if not isinstance(message, dict):
        return 0

    raw_id = message.get("id")
    raw_from = message.get("from")
    raw_type = message.get("type")

    if not isinstance(raw_id, str) or not raw_id:
        return 0
    if not isinstance(raw_from, str) or not raw_from:
        return 0
    if not isinstance(raw_type, str) or not raw_type:
        return 0

    message_id = raw_id[:100]
    sender_phone = raw_from[:20]
    message_type = raw_type[:20]

    message_body = None
    if message_type == "text":
        text_obj = message.get("text")
        if isinstance(text_obj, dict):
            raw_body = text_obj.get("body")
            if isinstance(raw_body, str):
                message_body = raw_body

    result = await session.execute(
        text(
            "INSERT INTO whatsapp_inbound_events "
            "(message_id, business_id, sender_phone, message_type, "
            " message_body, phone_number_id) "
            "VALUES (:mid, :bid, :phone, :mtype, :body, :pnid) "
            "ON CONFLICT (message_id) DO NOTHING"
        ),
        {
            "mid": message_id,
            "bid": business_id,
            "phone": sender_phone,
            "mtype": message_type,
            "body": message_body,
            "pnid": phone_number_id,
        },
    )
    inserted = result.rowcount or 0  # type: ignore[attr-defined]
    if inserted > 0:
        logger.info(
            "whatsapp_event_persisted",
            extra={"message_id": message_id, "business_id": business_id},
        )
    return inserted
