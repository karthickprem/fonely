"""Exotel telephony webhook handler — thin adapter for call tracking.

Receives call status webhooks and audio stream WebSocket connections
from Exotel. Audio processing will be wired to Pipecat by Dev4.
"""

import logging
from typing import Any

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from sqlalchemy import text

from fonely.services.exotel_config import ExotelNumberMapping

logger = logging.getLogger("fonely.api.channels.exotel")

router = APIRouter(prefix="/webhooks/exotel", tags=["exotel"])


def _get_mapping(app: object) -> ExotelNumberMapping:
    mapping = getattr(getattr(app, "state", None), "exotel_mapping", None)
    if mapping is None:
        mapping = ExotelNumberMapping()
    return mapping


@router.post("/call-status")
async def call_status_webhook(request: Request) -> Response:
    """Handle Exotel call status events: ringing, answered, completed, failed."""
    body: dict[str, Any] = await request.json()
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
