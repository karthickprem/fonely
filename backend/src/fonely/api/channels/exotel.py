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

import asyncio
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
from fonely.repositories.channel_identities import (
    PROVIDER_EXOTEL,
    ChannelIdentityRepository,
)
from fonely.services.audio_admission import AdmissionResult, admit_audio_stream
from fonely.services.audio_stream import (
    AudioStreamHandoff,
    MediaStreamStart,
    StreamStartRefusal,
    StreamStartResult,
    frame_event,
    parse_start_frame,
)

logger = logging.getLogger("fonely.api.channels.exotel")

router = APIRouter(prefix="/webhooks/exotel", tags=["exotel"])

_AUTH_QUERY_PARAM = "token"

# Query-string spellings for the provider call id, in preference order. When
# the console can template it into the media-stream URL we can refuse before
# completing the handshake, which is strictly better than refusing after.
_CALL_SID_QUERY_PARAMS = ("CallSid", "call_sid", "callsid")

# How long to wait for a socket to identify itself, and how many frames to
# read while waiting. Exotel sends a "connected" event before "start", so one
# frame is not enough; an unbounded read would let an authenticated-but-idle
# socket hold a worker indefinitely.
_OPENING_FRAME_TIMEOUT_SEC = 5.0
_MAX_OPENING_FRAMES = 5


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

    factory = request.app.state.session_factory
    async with factory() as session:
        # Which clinic this number reaches is a database fact as of migration
        # 0017. It used to be EXOTEL_NUMBER_MAPPINGS, which meant attaching a
        # clinic's signboard number required a redeploy and a JSON typo turned
        # every inbound call into "unknown number".
        business_id = await ChannelIdentityRepository(session).resolve_business_id(
            PROVIDER_EXOTEL, exotel_number
        )
        if business_id is None:
            logger.warning(
                "exotel_unknown_number",
                extra={"exotel_number": exotel_number, "call_sid": call_sid},
            )
            return Response(status_code=404, content="unknown number")

        if status == "ringing":
            # This is the observation the audio stream is later admitted
            # against, so it must survive provider retries without producing a
            # second call row. DO NOTHING plus the re-select makes a duplicate
            # delivery return the original call id rather than a new one.
            result = await session.execute(
                text(
                    "INSERT INTO calls "
                    "(business_id, caller_phone, call_provider, "
                    " provider_call_sid, started_at) "
                    "VALUES (:bid, :phone, :provider, :sid, NOW()) "
                    "ON CONFLICT (call_provider, provider_call_sid) "
                    "  WHERE provider_call_sid IS NOT NULL DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "bid": business_id,
                    "phone": caller_phone,
                    "provider": PROVIDER_EXOTEL,
                    "sid": call_sid,
                },
            )
            call_id = result.scalar_one_or_none()
            duplicate = call_id is None
            if duplicate:
                existing = await session.execute(
                    text(
                        "SELECT id FROM calls "
                        "WHERE call_provider = :provider "
                        "  AND provider_call_sid = :sid"
                    ),
                    {"provider": PROVIDER_EXOTEL, "sid": call_sid},
                )
                call_id = existing.scalar_one_or_none()
            await session.commit()
            logger.info(
                "exotel_call_ringing",
                extra={
                    "business_id": business_id,
                    "call_sid": call_sid,
                    "call_id": call_id,
                    "duplicate": duplicate,
                },
            )

        elif status == "completed":
            duration = body.get("Duration")
            # Correlate on the provider's own call id. The previous version
            # closed "the newest open call from this phone number", which ends
            # the wrong leg when a patient redials while the first is still
            # open. ended_at IS NULL keeps a retried completion from
            # overwriting the original end time and duration.
            result = await session.execute(
                text(
                    "UPDATE calls SET ended_at = NOW(), duration_sec = :dur "
                    "WHERE business_id = :bid "
                    "  AND call_provider = :provider "
                    "  AND provider_call_sid = :sid "
                    "  AND ended_at IS NULL"
                ),
                {
                    "bid": business_id,
                    "provider": PROVIDER_EXOTEL,
                    "sid": call_sid,
                    "dur": int(duration) if duration else None,
                },
            )
            await session.commit()
            logger.info(
                "exotel_call_completed",
                extra={
                    "business_id": business_id,
                    "call_sid": call_sid,
                    "rows_closed": result.rowcount,
                },
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


def _call_sid_from_query(websocket: WebSocket) -> str:
    for name in _CALL_SID_QUERY_PARAMS:
        value = websocket.query_params.get(name, "")
        if value:
            return str(value)
    return ""


async def _read_opening_frames(websocket: WebSocket) -> StreamStartResult:
    """Read control frames up to and including the start event.

    Bounded in both time and frame count: a socket holding the shared secret
    but never identifying itself must not pin a worker open.

    Every frame read is retained on the handoff. This loop is the only place
    that consumes frames before the runtime takes the socket, so "who ate the
    start event" has exactly one answer — and the answer is that nobody did,
    it was parsed and passed on.
    """
    seen: list[str] = []

    for _ in range(_MAX_OPENING_FRAMES):
        try:
            message = await asyncio.wait_for(
                websocket.receive(), timeout=_OPENING_FRAME_TIMEOUT_SEC
            )
        except TimeoutError:
            logger.warning("exotel_audio_stream_identify_timeout")
            return StreamStartResult(handoff=None, refusal=StreamStartRefusal.NO_START_EVENT)
        if message.get("type") == "websocket.disconnect":
            return StreamStartResult(handoff=None, refusal=StreamStartRefusal.NO_START_EVENT)

        raw = message.get("text")
        if raw is None:
            # Exotel's media stream is JSON text with base64 payloads. A binary
            # frame here is not something we know how to read, and guessing is
            # how a control frame ends up transcribed as speech.
            binary = message.get("bytes")
            if binary is None:
                continue
            raw = binary.decode("utf-8", errors="replace")

        seen.append(raw)
        event = frame_event(raw)

        if event == "media":
            # Audio before the format was declared. Decoding it means guessing
            # the sample rate, and a wrong guess is not silence — it is
            # plausible noise that STT will transcribe as words.
            logger.warning("exotel_audio_stream_media_before_start")
            return StreamStartResult(handoff=None, refusal=StreamStartRefusal.MEDIA_BEFORE_START)

        if event != "start":
            # "connected", and anything else the provider sends ahead of start.
            continue

        parsed = parse_start_frame(raw)
        if isinstance(parsed, StreamStartRefusal):
            logger.warning("exotel_audio_stream_bad_start", extra={"reason": parsed.value})
            return StreamStartResult(handoff=None, refusal=parsed)

        return StreamStartResult(
            handoff=AudioStreamHandoff(start=parsed, raw_frames=tuple(seen)),
            refusal=None,
        )

    logger.warning("exotel_audio_stream_identify_exhausted")
    return StreamStartResult(handoff=None, refusal=StreamStartRefusal.NO_START_EVENT)


async def _admit(websocket: WebSocket, call_sid: str) -> AdmissionResult:
    factory = websocket.app.state.session_factory
    async with factory() as session:
        return await admit_audio_stream(
            session, provider=PROVIDER_EXOTEL, provider_call_sid=call_sid
        )


@router.websocket("/audio-stream")
async def audio_stream(websocket: WebSocket) -> None:
    """Admit an Exotel audio stream, bound to the tenant that was dialed.

    Two gates, in this order:

    1. The shared secret, checked before accept(). Accepting first and closing
       on failure would complete the handshake, which both tells an
       unauthenticated caller the endpoint is real and hands them a connected
       socket for however long the close takes.

    2. Tenant admission, which resolves the clinic from a calls row our own
       ringing webhook wrote. The secret proves the connection came from the
       provider; it says nothing about which clinic the patient dialed, and
       trusting the socket's own claim would let one leaked applet URL reach
       every clinic in the system. See services/audio_admission.py.

    Where the call id comes from decides whether gate 2 can run before the
    handshake. If the console can template it into the media-stream URL we
    refuse without ever accepting; otherwise we accept and read it from the
    opening frames, which is the weaker form and is why the URL form is the
    documented configuration. Either way no audio is processed and no tenant
    context exists until admission succeeds.
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

    # Checked before the handshake, and a refusal rather than a drain. There
    # is no useful thing to do with a patient's call when no runtime is
    # mounted: draining it holds the line open, plays nothing, books nothing,
    # and looks in the logs exactly like a call that went fine. Absence must
    # not read as success. 1011 because an unmounted runtime is our
    # misconfiguration, not the caller's, and the applet's own failover should
    # take the call.
    runtime = getattr(websocket.app.state, "voice_audio_runtime", None)
    if runtime is None:
        logger.error("exotel_audio_stream_no_runtime")
        await websocket.close(code=WS_1011_INTERNAL_ERROR)
        return

    call_sid = _call_sid_from_query(websocket)
    identified_before_handshake = bool(call_sid)

    # The URL form lets us refuse without ever completing the handshake, which
    # is strictly better than refusing after. It does not exempt us from
    # reading the start event: the runtime needs the stream id and the declared
    # format either way.
    if identified_before_handshake:
        admission = await _admit(websocket, call_sid)
        if not admission.admitted:
            logger.warning(
                "exotel_audio_stream_refused",
                extra={
                    "reason": admission.refusal.value if admission.refusal else "",
                    "before_accept": True,
                },
            )
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()

        opening = await _read_opening_frames(websocket)
        if not opening.ok:
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return
    else:
        await websocket.accept()

        opening = await _read_opening_frames(websocket)
        if not opening.ok:
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return

        # The call id in the start event is a lookup key and nothing more:
        # admission resolves the tenant from the calls row our own ringing
        # webhook wrote, so a forged sid selects no row and is refused.
        handoff = opening.handoff
        assert handoff is not None  # ok is exactly "handoff is not None"
        # Not falling back to re-reading raw_frames[-1]: parse_start_frame
        # already pulled the id out of that exact frame, so a fallback could
        # only ever produce the same value. An empty id is passed through and
        # refused by admission as NO_CALL_IDENTIFIER, which is the honest
        # outcome — there is nowhere else on this socket for it to come from.
        call_sid = handoff.start.provider_call_sid
        admission = await _admit(websocket, call_sid)
        if not admission.admitted:
            logger.warning(
                "exotel_audio_stream_refused",
                extra={
                    "reason": admission.refusal.value if admission.refusal else "",
                    "before_accept": False,
                },
            )
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return

    session = admission.session
    assert session is not None  # admitted is exactly "session is not None"
    handoff = opening.handoff
    assert handoff is not None

    start: MediaStreamStart = handoff.start
    logger.info(
        "exotel_audio_stream_connected",
        extra={
            "business_id": session.business_id,
            "call_id": session.call_id,
            "identified_before_handshake": identified_before_handshake,
            "stream_sid": start.stream_sid,
            "encoding": start.encoding,
            "sample_rate": start.sample_rate,
        },
    )

    # The voice runtime is mounted by the application, not imported here, so
    # the telephony adapter stays free of pipeline dependencies and the runtime
    # is exercised against typed values rather than a raw socket. The handoff
    # carries the parsed start event and the raw text of every frame this
    # adapter consumed, so nothing the serializer needs was destroyed on the
    # way in.
    try:
        await runtime.handle_audio_session(websocket, session, handoff)
    except WebSocketDisconnect:
        logger.info("exotel_audio_stream_disconnected", extra={"call_id": session.call_id})
