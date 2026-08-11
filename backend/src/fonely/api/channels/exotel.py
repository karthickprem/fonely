"""Exotel telephony webhook handler — thin adapter for call tracking.

Receives call status webhooks and audio stream WebSocket connections
from Exotel. Audio processing will be wired to Pipecat by Dev4.

Both entry points authenticate. Neither used to: EXOTEL_WEBHOOK_SECRET
decided whether the router mounted and was then never read, so anyone who
found the URL could write `calls` rows for any clinic or hold open an audio
socket. That cost nothing while we were unreachable and becomes a way in the
moment we have a public address.

Exotel does not sign payloads the way Meta does -- there is no equivalent of
X-Hub-Signature-256 to verify -- so the shared secret has to travel in the
request itself. Which carrier it arrives in depends on how the applet is
configured in the Exotel console, so all three it can produce are accepted
and every one of them is compared in constant time:

  * HTTP Basic, from credentials embedded in the callback URL. Preferred for
    the status callback: it stays out of query strings.
  * Bearer, where a custom Authorization header can be set.
  * A `token` query parameter. Often the only carrier available for the
    media-stream applet, which takes a URL and gives no header control. It
    is the weakest of the three because URLs get written to proxy and access
    logs, so prefer either of the others wherever the console allows it.

Before go-live the carrier the account actually sends must be confirmed
against the Exotel console rather than assumed from this list.
"""

import base64
import binascii
import hmac
import json
import logging
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from sqlalchemy import text

# Rejecting a WebSocket before the handshake completes is a close with no
# preceding accept. 1008 is "policy violation"; 1011 is "internal error",
# used when our own secret is missing, because that is our fault and not the
# caller's and the two must not be reported as the same thing.
from starlette.status import WS_1008_POLICY_VIOLATION, WS_1011_INTERNAL_ERROR

from fonely.core.config import settings
from fonely.services.exotel_config import ExotelNumberMapping

logger = logging.getLogger("fonely.api.channels.exotel")

router = APIRouter(prefix="/webhooks/exotel", tags=["exotel"])

_AUTH_QUERY_PARAM = "token"


def _get_mapping(app: object) -> ExotelNumberMapping:
    mapping = getattr(getattr(app, "state", None), "exotel_mapping", None)
    if mapping is None:
        mapping = ExotelNumberMapping()
    return mapping


def _matches(presented: str, expected: str) -> bool:
    """Constant-time equality that never raises on odd input.

    compare_digest rejects str containing non-ASCII, and a caller controls
    what arrives here, so an exotic header would otherwise surface as a 500 —
    a way to tell valid-shaped credentials from invalid ones without knowing
    the secret.
    """
    if not presented or not expected:
        return False
    try:
        return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    except (UnicodeEncodeError, TypeError):
        return False


def _presented_credentials(auth_header: str, token_param: str) -> list[str]:
    """Every secret the request could be offering, in no particular order."""
    candidates: list[str] = []

    if token_param:
        candidates.append(token_param)

    scheme, _, value = auth_header.partition(" ")
    scheme, value = scheme.lower(), value.strip()

    if scheme == "bearer" and value:
        candidates.append(value)
    elif scheme == "basic" and value:
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return candidates
        # The secret may have been configured as the whole user:pass pair or
        # as the password against some throwaway username. Accept either.
        candidates.append(decoded)
        _, separator, password = decoded.partition(":")
        if separator and password:
            candidates.append(password)

    return candidates


def _is_authenticated(auth_header: str, token_param: str) -> bool:
    expected = settings.exotel_webhook_secret
    if not expected:
        return False

    # Deliberately not short-circuiting on the first match: `any()` over a
    # generator would stop early, making the time taken depend on which
    # carrier held the secret.
    matched = False
    for candidate in _presented_credentials(auth_header, token_param):
        if _matches(candidate, expected):
            matched = True
    return matched


async def _read_params(request: Request) -> dict[str, Any]:
    """Read the callback body as either form fields or JSON.

    Exotel posts status callbacks as application/x-www-form-urlencoded, and
    this handler only ever parsed JSON -- so a real callback would have died
    in request.json() before reaching any of the logic below. Every existing
    test posted JSON, which is why nothing caught it.

    The urlencoded body is decoded here rather than through request.form(),
    which asserts on python-multipart being installed. It is not a dependency
    and adding one to read a flat key-value body would mean touching a lock
    file CI verifies with --locked, for no gain. multipart/form-data is
    therefore not accepted -- Exotel does not send it for callbacks.

    A body that parses as neither yields no parameters rather than raising,
    which the missing-field check below turns into a 400.
    """
    content_type = request.headers.get("content-type", "")
    raw = await request.body()

    if "application/x-www-form-urlencoded" in content_type:
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {}
        return dict(parse_qsl(decoded, keep_blank_values=True))

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@router.post("/call-status")
async def call_status_webhook(request: Request) -> Response:
    """Handle Exotel call status events: ringing, answered, completed, failed."""
    if not settings.exotel_webhook_secret:
        # Unreachable while app.py gates the router on this same setting;
        # kept so the handler is safe if it is ever mounted another way.
        logger.error("exotel_webhook_secret_not_configured")
        return Response(status_code=503, content="not configured")

    if not _is_authenticated(
        request.headers.get("Authorization", ""),
        request.query_params.get(_AUTH_QUERY_PARAM, ""),
    ):
        # Nothing about the presented credential is logged -- a rejected
        # secret is still a secret, and this line lands in shared logs.
        logger.warning("exotel_webhook_unauthenticated", extra={"route": "call-status"})
        return Response(status_code=401, content="unauthorized")

    body = await _read_params(request)
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

    For now: authenticate, accept, log, and drain. Actual audio processing
    will be wired to the Pipecat pipeline by Dev4, behind this same guard --
    the seam the voice runtime mounts on is the authenticated one, never the
    bare socket.

    The credential is checked before accept() rather than after. Accepting
    first and closing on failure would complete the handshake, which both
    tells an unauthenticated caller the endpoint is real and gives them a
    connected socket for however long the close takes.
    """
    if not settings.exotel_webhook_secret:
        logger.error("exotel_webhook_secret_not_configured")
        await websocket.close(code=WS_1011_INTERNAL_ERROR)
        return

    if not _is_authenticated(
        websocket.headers.get("Authorization", ""),
        websocket.query_params.get(_AUTH_QUERY_PARAM, ""),
    ):
        logger.warning("exotel_audio_stream_unauthenticated")
        await websocket.close(code=WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    logger.info("exotel_audio_stream_connected")
    try:
        while True:
            await websocket.receive_bytes()
    except WebSocketDisconnect:
        logger.info("exotel_audio_stream_disconnected")
