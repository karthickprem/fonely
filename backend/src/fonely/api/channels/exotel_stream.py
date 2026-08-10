"""Authenticated Exotel WebSocket entrypoint and Pipecat handoff.

The provider edge owns gateway authentication, start-event validation,
trusted tenant/call binding, admission, correlation registration, and
cleanup. Pipecat owns audio serialization and the voice pipeline.

This router is mounted only when its production prerequisites are wired.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import APIRouter, WebSocket
from pipecat.serializers.exotel import ExotelFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from starlette.websockets import WebSocketDisconnect

from fonely.api.channels.exotel_admission import (
    StreamAdmissionController,
    resolve_business_id,
    verify_gateway_secret,
)
from fonely.domain.calls.correlation import CallCorrelationStore, CorrelationRecord
from fonely.services.exotel_config import ExotelNumberMapping

logger = logging.getLogger("fonely.api.channels.exotel_stream")

router = APIRouter(prefix="/webhooks/exotel", tags=["exotel"])

_MAX_START_MESSAGE = 16_384
_SUPPORTED_RATES = frozenset({8000, 16000, 24000})
_SUPPORTED_CODECS = frozenset({"audio/x-raw"})
_HANDSHAKE_TIMEOUT_S = 10


class ExotelStartValidationError(Exception):
    """Start event validation failed before the runtime was constructed."""


@dataclass(frozen=True, slots=True)
class ExotelStartMetadata:
    stream_sid: str
    call_sid: str
    account_sid: str
    from_number: str
    to_number: str
    direction: str | None
    sample_rate: int


@dataclass(frozen=True, slots=True)
class ExotelStreamSession:
    business_id: int
    metadata: ExotelStartMetadata
    expected_sample_rate: int
    provisioning_drift: bool


class ExotelRuntimeFactory(Protocol):
    """Starts the existing transport-agnostic Pipecat voice pipeline."""

    async def __call__(
        self,
        transport: FastAPIWebsocketTransport,
        session: ExotelStreamSession,
    ) -> None: ...


def validate_start_event(msg: dict[str, Any]) -> ExotelStartMetadata:
    """Validate immutable Exotel stream metadata; never infer a rate."""
    if msg.get("event") != "start":
        raise ExotelStartValidationError("first media event must be start")

    start_data = msg.get("start")
    if not isinstance(start_data, dict):
        raise ExotelStartValidationError("missing or invalid start payload")

    def required_string(field: str) -> str:
        value = start_data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ExotelStartValidationError(f"missing or invalid {field}")
        return value.strip()

    stream_sid = required_string("stream_sid")
    call_sid = required_string("call_sid")
    account_sid = required_string("account_sid")
    from_number = required_string("from")
    to_number = required_string("to")

    direction_value = start_data.get("direction")
    if direction_value is not None and not isinstance(direction_value, str):
        raise ExotelStartValidationError("invalid direction")
    direction = direction_value.strip().lower() if direction_value else None

    media_format = start_data.get("media_format")
    if not isinstance(media_format, dict):
        raise ExotelStartValidationError("missing media_format in start")

    encoding = media_format.get("encoding")
    if encoding not in _SUPPORTED_CODECS:
        raise ExotelStartValidationError(f"unsupported codec: {encoding!r}")

    raw_rate = media_format.get("sample_rate")
    if raw_rate is None or isinstance(raw_rate, bool):
        raise ExotelStartValidationError("missing or malformed sample_rate")
    try:
        sample_rate = int(raw_rate)
    except (ValueError, TypeError) as exc:
        raise ExotelStartValidationError(
            f"malformed sample_rate: {raw_rate!r}"
        ) from exc
    if sample_rate not in _SUPPORTED_RATES:
        raise ExotelStartValidationError(f"unsupported sample_rate: {sample_rate}")

    channels = media_format.get("channels")
    if channels is not None and str(channels) != "1":
        raise ExotelStartValidationError(
            f"unsupported channels: {channels} (expected mono)"
        )

    return ExotelStartMetadata(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=account_sid,
        from_number=from_number,
        to_number=to_number,
        direction=direction,
        sample_rate=sample_rate,
    )


def check_rate_drift(declared_rate: int, expected_rate: int) -> bool:
    """Supported disagreement is provisioning drift, not silent defaulting."""
    return declared_rate != expected_rate


def parse_ws_start_message(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > _MAX_START_MESSAGE:
        raise ExotelStartValidationError("start message too large")
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExotelStartValidationError("invalid JSON") from exc
    if not isinstance(msg, dict):
        raise ExotelStartValidationError("expected JSON object")
    return msg


async def _receive_start(websocket: WebSocket) -> ExotelStartMetadata:
    """Require connected? then exactly one start before any media."""
    try:
        async with asyncio.timeout(_HANDSHAKE_TIMEOUT_S):
            first = parse_ws_start_message(await websocket.receive_text())
            if first.get("event") == "connected":
                first = parse_ws_start_message(await websocket.receive_text())
    except TimeoutError as exc:
        raise ExotelStartValidationError("start handshake timeout") from exc
    return validate_start_event(first)


@router.websocket("/media")
async def exotel_media_websocket(websocket: WebSocket) -> None:
    """Authenticate and admit one Exotel stream into the Pipecat pipeline."""
    state = websocket.app.state
    secret = getattr(state, "exotel_gateway_secret", "")
    if not verify_gateway_secret(websocket.headers, secret):
        await websocket.close(code=4401, reason="unauthorized")
        return

    mapping: ExotelNumberMapping | None = getattr(state, "exotel_mapping", None)
    correlation: CallCorrelationStore | None = getattr(
        state, "exotel_correlation", None
    )
    admission: StreamAdmissionController | None = getattr(
        state, "exotel_admission", None
    )
    runtime_factory: ExotelRuntimeFactory | None = getattr(
        state, "exotel_runtime_factory", None
    )
    expected_account = getattr(state, "exotel_account_id", "")
    environment = getattr(state, "exotel_environment", "")
    expected_rate = getattr(state, "exotel_expected_sample_rate", 0)

    if (
        mapping is None
        or mapping.is_empty()
        or correlation is None
        or admission is None
        or runtime_factory is None
        or not expected_account
        or environment not in {"sandbox", "production"}
        or expected_rate not in _SUPPORTED_RATES
    ):
        await websocket.close(code=1013, reason="service unavailable")
        return

    admitted_business_id: int | None = None
    await websocket.accept()
    try:
        metadata = await _receive_start(websocket)
        if metadata.account_sid != expected_account:
            raise ExotelStartValidationError("provider account mismatch")

        business_id = resolve_business_id(
            mapping, metadata.to_number, metadata.from_number
        )
        if business_id is None:
            await websocket.close(code=4404, reason="unknown tenant")
            return

        decision = admission.try_admit(str(business_id))
        if not decision.admitted:
            await websocket.close(code=4429, reason=decision.reason)
            return
        admitted_business_id = business_id

        await correlation.register_admitted_call(
            CorrelationRecord(
                provider="exotel",
                provider_account_id=metadata.account_sid,
                provider_call_id=metadata.call_sid,
                called_number=metadata.to_number,
                business_id=business_id,
                direction=metadata.direction,
            )
        )

        provisioning_drift = check_rate_drift(
            metadata.sample_rate, expected_rate
        )
        if provisioning_drift:
            logger.error(
                "exotel_stream_provisioning_drift",
                extra={
                    "business_id": business_id,
                    "declared_rate": metadata.sample_rate,
                    "expected_rate": expected_rate,
                },
            )

        serializer = ExotelFrameSerializer(
            stream_sid=metadata.stream_sid,
            call_sid=metadata.call_sid,
            params=ExotelFrameSerializer.InputParams(
                exotel_sample_rate=metadata.sample_rate,
                sample_rate=16000,
            ),
        )
        transport = FastAPIWebsocketTransport(
            websocket,
            FastAPIWebsocketParams(
                serializer=serializer,
                session_timeout=getattr(
                    state, "exotel_session_timeout_seconds", 3600
                ),
                allowed_origins=[],
            ),
        )
        await runtime_factory(
            transport,
            ExotelStreamSession(
                business_id=business_id,
                metadata=metadata,
                expected_sample_rate=expected_rate,
                provisioning_drift=provisioning_drift,
            ),
        )
    except ExotelStartValidationError as exc:
        logger.warning("exotel_stream_protocol_error", extra={"error": str(exc)})
        await websocket.close(code=4400, reason="protocol error")
    except WebSocketDisconnect:
        pass
    finally:
        if admitted_business_id is not None:
            admission.release(str(admitted_business_id))
