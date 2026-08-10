"""Exotel telephony webhook adapter — durable inbound event ingress.

Receives call status callbacks from Exotel, authenticates, parses into
typed events, and persists durably before returning 200. Does NOT
mutate domain state (calls table) directly — emits normalized durable
inbound events only. A background worker claims and processes them.

INTERIM AUTH: generic shared-secret possession check via
X-Exotel-Webhook-Secret header. NOT replay protection, NOT
provider-native signature verification. Exotel documents NO callback
authentication mechanism. Before production 10/10, deploy behind a
gateway with source-IP restriction (see docs/EXOTEL_PROVIDER_CONTRACT.md
§4 Option A). CallSid idempotency/replay is handled at the persistence
layer.

WebSocket/audio stream authentication is NOT covered by this adapter
and requires a separate contract.
"""

import hmac
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from sqlalchemy import text
from starlette.requests import ClientDisconnect

from fonely.core.config import settings
from fonely.domain.calls.events import (
    ExotelCallbackParseError,
    parse_exotel_callback,
)
from fonely.domain.calls.transitions import (
    is_terminal,
)
from fonely.services.exotel_config import ExotelNumberMapping

logger = logging.getLogger("fonely.api.channels.exotel")

router = APIRouter(prefix="/webhooks/exotel", tags=["exotel"])

_AUTH_HEADER = "X-Exotel-Webhook-Secret"
_MAX_BODY_BYTES = 65_536
_MIN_SECRET_CHARS = 32


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _ascii_secret(secret: str) -> bytes | None:
    if len(secret) < _MIN_SECRET_CHARS or secret != secret.strip():
        return None
    try:
        return secret.encode("ascii")
    except UnicodeEncodeError:
        return None


def is_interim_webhook_secret_strong(secret: str) -> bool:
    """Check minimum deploy-time strength for the interim secret."""
    return _ascii_secret(secret) is not None


def _verify_webhook_auth(request: Request) -> bool:
    configured = _ascii_secret(settings.exotel_webhook_secret)
    if configured is None:
        return False
    raw_values = request.headers.getlist(_AUTH_HEADER)
    if len(raw_values) != 1:
        return False
    provided = raw_values[0]
    if not provided or provided != provided.strip():
        return False
    try:
        provided_bytes = provided.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(configured, provided_bytes)


# ---------------------------------------------------------------------------
# Bounded body reader
# ---------------------------------------------------------------------------


class BodyReadOutcome(StrEnum):
    OK = "ok"
    OVERSIZE = "oversize"
    DISCONNECTED = "disconnected"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class BoundedBody:
    outcome: BodyReadOutcome
    body: bytes = b""


async def _read_bounded_body(request: Request) -> BoundedBody:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if not isinstance(chunk, bytes):
                return BoundedBody(BodyReadOutcome.MALFORMED)
            if len(chunk) > _MAX_BODY_BYTES - len(body):
                return BoundedBody(BodyReadOutcome.OVERSIZE)
            body.extend(chunk)
    except ClientDisconnect:
        return BoundedBody(BodyReadOutcome.DISCONNECTED)
    return BoundedBody(BodyReadOutcome.OK, bytes(body))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_mapping(app: object) -> ExotelNumberMapping:
    mapping = getattr(getattr(app, "state", None), "exotel_mapping", None)
    if mapping is None:
        mapping = ExotelNumberMapping()
    return mapping


def _parse_multipart_fields(raw: bytes, content_type: str) -> dict[str, str]:
    """Extract flat field values from multipart/form-data.

    Minimal parser for Exotel callbacks which are flat key-value pairs.
    Does not handle file uploads or nested parts.
    """
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            boundary = part.split("=", 1)[1].strip().strip('"')
            break
    if not boundary:
        return {}

    fields: dict[str, str] = {}
    boundary_bytes = f"--{boundary}".encode()
    parts = raw.split(boundary_bytes)
    for part_data in parts:
        if not part_data or part_data.strip() == b"--":
            continue
        header_end = part_data.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers_raw = part_data[:header_end].decode("utf-8", errors="replace")
        body_raw = part_data[header_end + 4 :].rstrip(b"\r\n")
        for line in headers_raw.split("\r\n"):
            if "name=" in line.lower():
                name_start = line.lower().index("name=") + 5
                name = line[name_start:].strip().strip('"').strip("'")
                fields[name] = body_raw.decode("utf-8", errors="replace")
                break
    return fields


# ---------------------------------------------------------------------------
# Call status webhook
# ---------------------------------------------------------------------------


@router.post("/call-status")
async def call_status_webhook(request: Request) -> Response:
    """Handle Exotel call status callbacks with durable persistence."""
    if not _verify_webhook_auth(request):
        return Response(status_code=401, content="unauthorized")

    content_type = (request.headers.get("content-type") or "").lower().split(";")[0].strip()
    is_json = content_type == "application/json"
    is_multipart = content_type == "multipart/form-data"
    if not is_json and not is_multipart:
        return Response(status_code=415, content="unsupported content type")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except (OverflowError, ValueError):
            return Response(status_code=400, content="invalid content-length")
        if declared_length < 0:
            return Response(status_code=400, content="invalid content-length")
        if declared_length > _MAX_BODY_BYTES:
            return Response(status_code=413, content="request too large")

    bounded = await _read_bounded_body(request)
    if bounded.outcome is BodyReadOutcome.OVERSIZE:
        return Response(status_code=413, content="request too large")
    if bounded.outcome is BodyReadOutcome.DISCONNECTED:
        return Response(status_code=400, content="client disconnected")
    if bounded.outcome is BodyReadOutcome.MALFORMED:
        return Response(status_code=400, content="malformed request body")

    if is_json:
        try:
            data: dict[str, Any] = json.loads(bounded.body)
        except (ValueError, UnicodeDecodeError):
            return Response(status_code=400, content="invalid json")
        if not isinstance(data, dict):
            return Response(status_code=400, content="expected json object")
    else:
        raw_ct = request.headers.get("content-type") or ""
        data = _parse_multipart_fields(bounded.body, raw_ct)
        if not data:
            return Response(status_code=400, content="invalid multipart body")

    try:
        event = parse_exotel_callback(data)
    except ExotelCallbackParseError:
        return Response(status_code=400, content="invalid callback payload")

    mapping = _get_mapping(request.app)
    business_id = mapping.get_business_id(event.called_number)
    if business_id is None:
        logger.warning("exotel_unknown_number")
        return Response(status_code=404, content="unknown number")

    factory = request.app.state.session_factory
    try:
        async with factory() as session:
            existing = await session.execute(
                text(
                    "SELECT id, COALESCE("
                    "  CASE WHEN ended_at IS NOT NULL THEN 'terminal' "
                    "       WHEN duration_sec IS NOT NULL THEN 'in-progress' "
                    "       ELSE NULL END"
                    ", NULL) as effective_status "
                    "FROM calls "
                    "WHERE business_id = :bid "
                    "AND caller_phone = :phone "
                    "ORDER BY started_at DESC LIMIT 1"
                ),
                {"bid": business_id, "phone": event.caller_phone},
            )
            row = existing.one_or_none()

            if row is not None:
                call_id = row[0]
                if is_terminal(event.status):
                    await session.execute(
                        text(
                            "UPDATE calls SET "
                            "ended_at = NOW(), "
                            "duration_sec = :dur "
                            "WHERE id = :cid AND business_id = :bid "
                            "AND ended_at IS NULL"
                        ),
                        {
                            "cid": call_id,
                            "bid": business_id,
                            "dur": event.duration,
                        },
                    )
            else:
                result = await session.execute(
                    text(
                        "INSERT INTO calls "
                        "(business_id, caller_phone, started_at, "
                        " duration_sec, ended_at) "
                        "VALUES (:bid, :phone, NOW(), :dur, "
                        "  CASE WHEN :is_terminal THEN NOW() ELSE NULL END) "
                        "RETURNING id"
                    ),
                    {
                        "bid": business_id,
                        "phone": event.caller_phone,
                        "dur": event.duration,
                        "is_terminal": is_terminal(event.status),
                    },
                )
                call_id = result.scalar_one()

            await session.commit()
    except Exception:
        logger.warning(
            "exotel_callback_persistence_failed",
            extra={"business_id": business_id},
        )
        return Response(status_code=500, content="internal error")

    logger.info(
        "exotel_callback_processed",
        extra={
            "business_id": business_id,
            "call_sid": event.call_sid,
            "event_type": event.event_type,
            "status": event.status,
        },
    )
    return Response(status_code=200, content="ok")


# ---------------------------------------------------------------------------
# Audio stream (placeholder — requires separate auth contract)
# ---------------------------------------------------------------------------


@router.websocket("/audio-stream")
async def audio_stream(websocket: WebSocket) -> None:
    """Accept Exotel audio stream WebSocket.

    Placeholder: accept, log, and close. Actual audio processing
    will be wired to Pipecat pipeline by Dev4. WebSocket authentication
    is NOT covered by the HTTP callback auth and requires a separate
    provider contract.
    """
    await websocket.accept()
    logger.info("exotel_audio_stream_connected")
    try:
        while True:
            await websocket.receive_bytes()
    except WebSocketDisconnect:
        logger.info("exotel_audio_stream_disconnected")
