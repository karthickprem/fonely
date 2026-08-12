"""VoiceAudioRuntime: the entry point that turns an admitted media stream into a
running Pipecat voice call.

Scope of this module (step 4): construct the per-call Exotel serializer and the
FastAPI websocket transport from the ADMITTED handoff — the validated
``MediaStreamStart`` — and expose the seam the enforced open-order + lifecycle
build on next. Identity comes only from the ``AudioSession``; wire parameters
come only from ``handoff.start``.

Two rules, both testable against the CONSTRUCTED objects (never the inputs):

  * The serializer's decode rate is ``start.sample_rate`` verbatim — no literal
    is hardcoded anywhere downstream of the parse. A hardcoded rate against a
    16 kHz applet does not fail loudly; it decodes plausible noise that STT
    turns into words, and the first symptom is a booking from something nobody
    said. ``build_serializer`` is the single place the rate is applied.
  * The outbound stream handle is ``start.stream_sid`` from the typed field,
    quoted back so the provider routes our audio to the right call. It is never
    taken by re-serializing an inbound frame.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pipecat.serializers.exotel import ExotelFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

    from .media_stream_types import MediaStreamStart

# Outbound audio rate to the provider. Exotel accepts a fixed set; this is the
# rate WE emit, independent of the inbound wire rate (which comes from the
# admitted start event). It is an output-side constant, not the decode rate.
_AUDIO_OUT_SAMPLE_RATE = 8000


def build_serializer(start: MediaStreamStart) -> ExotelFrameSerializer:
    """Construct the per-call serializer from the admitted start event.

    The decode (wire) rate is taken ONLY from ``start.sample_rate`` — the
    validated rate the provider declared for this stream. No literal rate is
    substituted here; a 16 kHz stream is decoded at 16 kHz. The outbound handle
    is ``start.stream_sid`` so our media frames quote the right stream back.
    """
    return ExotelFrameSerializer(
        stream_sid=start.stream_sid,
        params=ExotelFrameSerializer.InputParams(
            exotel_sample_rate=start.sample_rate,
            sample_rate=start.sample_rate,
        ),
    )


def build_transport(websocket: WebSocket, start: MediaStreamStart) -> FastAPIWebsocketTransport:
    """Construct the duplex FastAPI websocket transport for one admitted call,
    wired to the per-call serializer. ``.input()`` and ``.output()`` slot into
    the assembled pipeline."""
    params = FastAPIWebsocketParams(
        serializer=build_serializer(start),
        allowed_origins=[],
        audio_out_sample_rate=_AUDIO_OUT_SAMPLE_RATE,
    )
    return FastAPIWebsocketTransport(websocket=websocket, params=params)
