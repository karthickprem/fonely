"""WhatsApp webhook handler — durable inbound event pattern.

Messages are persisted to the whatsapp_inbound_events table before
returning 200 to Meta. A separate inbound worker processes them with
retry. No message is permanently lost as long as the database is
available.
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
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    metadata = value.get("metadata", {})
                    phone_number_id = metadata.get("phone_number_id", "")

                    business_id = mapping.get_business_id(phone_number_id)
                    if business_id is None:
                        continue

                    for message in value.get("messages", []):
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
    message: dict[str, object],
    business_id: int,
    phone_number_id: str,
) -> int:
    message_id = str(message.get("id", ""))
    sender_phone = str(message.get("from", ""))
    message_type = str(message.get("type", ""))

    if not message_id or not sender_phone:
        return 0

    message_body = None
    if message_type == "text":
        text_obj = message.get("text")
        if isinstance(text_obj, dict):
            message_body = str(text_obj.get("body", ""))

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
