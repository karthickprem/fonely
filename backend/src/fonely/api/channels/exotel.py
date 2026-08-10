"""Exotel telephony webhook adapter — thin ingress to typed intake.

Authenticates, parses, validates, and delegates to InboundCallEventIntake.
Does NOT mutate domain state. Does NOT import sqlalchemy.

INTERIM AUTH: shared-secret possession check. Exotel documents NO native
callback authentication. See docs/EXOTEL_PROVIDER_CONTRACT.md §4.

Audio WebSocket is NOT exposed — it requires a separate auth contract.
"""

import hmac
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.requests import ClientDisconnect

from fonely.core.config import settings
from fonely.domain.calls.events import (
    ExotelCallbackParseError,
    parse_exotel_callback,
)
from fonely.domain.calls.intake import (
    ConflictingCallEventError,
    DuplicateCallEventError,
    InboundCallEventIntake,
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


def _get_intake(app: object) -> InboundCallEventIntake | None:
    return getattr(getattr(app, "state", None), "exotel_intake", None)


def _parse_multipart_fields(raw: bytes, content_type: str) -> dict[str, str]:
    """Extract flat scalar field values from multipart/form-data.

    Uses Python email.parser for standards-compliant Content-Disposition
    parsing. Rejects file uploads (parts with filename). Preserves exact
    field values including CR/LF in text.
    """
    from email.parser import BytesParser
    from email.policy import HTTP

    full_headers = f"Content-Type: {content_type}\r\n\r\n".encode() + raw
    msg = BytesParser(policy=HTTP).parsebytes(full_headers)

    if not msg.is_multipart():
        return {}

    fields: dict[str, str] = {}
    for part in msg.iter_parts():
        cd = part.get("Content-Disposition", "")
        if "filename" in cd.lower():
            continue
        name = part.get_param("name", header="Content-Disposition")
        if name is None:
            continue
        if name in fields:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        try:
            fields[str(name)] = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return fields


def _resolve_business_id(mapping: ExotelNumberMapping, called: str, caller: str) -> int | None:
    """Tenant routing — direction-neutral, ambiguity-rejecting.

    Exact callback field semantics for From/To are sandbox-unverified
    (OQ-1). Try both numbers against the mapping. If both map to the
    same business, accept. If both map to different businesses, reject
    as ambiguous. If exactly one maps, accept that one.
    """
    to_bid = mapping.get_business_id(called)
    from_bid = mapping.get_business_id(caller)
    if to_bid is not None and from_bid is not None and to_bid != from_bid:
        return None
    return to_bid or from_bid


# ---------------------------------------------------------------------------
# Call status webhook
# ---------------------------------------------------------------------------


@router.post("/call-status")
async def call_status_webhook(request: Request) -> Response:
    """Authenticate, parse, validate, persist via intake, return 200."""
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
    business_id = _resolve_business_id(mapping, event.called_number, event.caller_phone)
    if business_id is None:
        logger.warning("exotel_unknown_number")
        return Response(status_code=404, content="unknown number")

    intake = _get_intake(request.app)
    if intake is None:
        logger.error("exotel_intake_not_configured")
        return Response(status_code=503, content="service unavailable")

    inbound_event = event.to_inbound_event()
    try:
        await intake.persist(business_id, inbound_event)
    except DuplicateCallEventError:
        logger.info(
            "exotel_callback_duplicate",
            extra={
                "business_id": business_id,
                "call_sid": event.call_sid,
                "event_type": event.event_type,
            },
        )
        return Response(status_code=200, content="ok")
    except ConflictingCallEventError:
        logger.warning(
            "exotel_callback_conflict",
            extra={
                "business_id": business_id,
                "call_sid": event.call_sid,
                "event_type": event.event_type,
            },
        )
        return Response(status_code=409, content="conflicting event")
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


# Audio WebSocket is NOT exposed — requires separate auth contract.
# See docs/EXOTEL_PROVIDER_CONTRACT.md §4.
