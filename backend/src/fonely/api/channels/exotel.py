"""Exotel telephony webhook handler — thin adapter for call tracking.

Receives call status webhooks and audio stream WebSocket connections
from Exotel. Audio processing will be wired to Pipecat by Dev4.

INTERIM AUTH: generic shared-secret possession check via
X-Exotel-Webhook-Secret header. This is NOT replay protection,
NOT provider-native signature verification, and does NOT
authenticate the WebSocket/media stream. Before production 10/10,
replace with Exotel-native request signing once the provider
contract specifies it. CallSid idempotency/replay remains open.

Deployment requires a high-entropy secret (>= 32 random chars)
rotated on a documented schedule. See ops runbook for rotation.
"""

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from sqlalchemy import text

from fonely.core.config import settings
from fonely.services.exotel_config import ExotelNumberMapping

logger = logging.getLogger("fonely.api.channels.exotel")

router = APIRouter(prefix="/webhooks/exotel", tags=["exotel"])

_AUTH_HEADER = "X-Exotel-Webhook-Secret"
_MAX_BODY_BYTES = 65_536


def _verify_webhook_auth(request: Request) -> bool:
    """Constant-time comparison of the interim shared-secret header.

    Rejects duplicate/ambiguous auth headers, leading/trailing whitespace,
    and empty values. Never logs or returns the secret or header value.
    """
    configured = settings.exotel_webhook_secret
    if not configured:
        return False
    raw_values = request.headers.getlist(_AUTH_HEADER)
    if len(raw_values) != 1:
        return False
    provided = raw_values[0]
    if not provided or provided != provided.strip():
        return False
    return hmac.compare_digest(configured, provided)


def _get_mapping(app: object) -> ExotelNumberMapping:
    mapping = getattr(getattr(app, "state", None), "exotel_mapping", None)
    if mapping is None:
        mapping = ExotelNumberMapping()
    return mapping


@router.post("/call-status")
async def call_status_webhook(request: Request) -> Response:
    """Handle Exotel call status events: ringing, answered, completed, failed."""
    if not _verify_webhook_auth(request):
        return Response(status_code=401, content="unauthorized")

    content_type = (request.headers.get("content-type") or "").lower().split(";")[0].strip()
    if content_type != "application/json":
        return Response(status_code=415, content="unsupported content type")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                return Response(status_code=413, content="request too large")
        except ValueError:
            return Response(status_code=400, content="invalid content-length")

    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        return Response(status_code=413, content="request too large")

    import json as _json

    try:
        body: dict[str, Any] = _json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=400, content="invalid json")

    if not isinstance(body, dict):
        return Response(status_code=400, content="expected json object")
    call_sid = str(body.get("CallSid", ""))
    status = str(body.get("Status", "")).lower()
    exotel_number = str(body.get("To", ""))
    caller_phone = str(body.get("From", ""))

    if not call_sid or not status:
        return Response(status_code=400, content="missing CallSid or Status")

    mapping = _get_mapping(request.app)
    business_id = mapping.get_business_id(exotel_number)
    if business_id is None:
        logger.warning(
            "exotel_unknown_number",
            extra={"exotel_number": exotel_number, "call_sid": call_sid},
        )
        return Response(status_code=404, content="unknown number")

    factory = request.app.state.session_factory
    async with factory() as session:
        if status == "ringing":
            result = await session.execute(
                text(
                    "INSERT INTO calls (business_id, caller_phone, started_at) "
                    "VALUES (:bid, :phone, NOW()) "
                    "RETURNING id"
                ),
                {"bid": business_id, "phone": caller_phone},
            )
            call_id = result.scalar_one()
            await session.commit()
            logger.info(
                "exotel_call_ringing",
                extra={
                    "business_id": business_id,
                    "call_sid": call_sid,
                    "call_id": call_id,
                },
            )

        elif status == "completed":
            duration = body.get("Duration")
            await session.execute(
                text(
                    "UPDATE calls SET ended_at = NOW(), duration_sec = :dur "
                    "WHERE id = ("
                    "  SELECT id FROM calls "
                    "  WHERE business_id = :bid AND caller_phone = :phone "
                    "  AND ended_at IS NULL "
                    "  ORDER BY started_at DESC LIMIT 1"
                    ")"
                ),
                {
                    "bid": business_id,
                    "phone": caller_phone,
                    "dur": int(duration) if duration else None,
                },
            )
            await session.commit()
            logger.info(
                "exotel_call_completed",
                extra={"business_id": business_id, "call_sid": call_sid},
            )

        elif status in ("answered", "failed"):
            logger.info(
                "exotel_call_status",
                extra={
                    "business_id": business_id,
                    "call_sid": call_sid,
                    "status": status,
                },
            )

    return Response(status_code=200, content="ok")


@router.websocket("/audio-stream")
async def audio_stream(websocket: WebSocket) -> None:
    """Accept Exotel audio stream WebSocket.

    For now: accept, log, and close. Actual audio processing
    will be wired to Pipecat pipeline by Dev4.
    """
    await websocket.accept()
    logger.info("exotel_audio_stream_connected")
    try:
        while True:
            await websocket.receive_bytes()
    except WebSocketDisconnect:
        logger.info("exotel_audio_stream_disconnected")
