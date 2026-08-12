"""VoiceAudioRuntime: the entry point that turns an admitted media stream into a
running Pipecat voice call.

Identity comes only from the ``AudioSession``; wire parameters come only from
``handoff.start``. The runtime never re-does admission or tenant lookup — it
trusts the admitted session it is handed.

Two rules, both testable against the CONSTRUCTED objects (never the inputs):

  * The serializer's decode rate is ``start.sample_rate`` verbatim — no literal
    is hardcoded anywhere downstream of the parse. A hardcoded rate against a
    16 kHz applet does not fail loudly; it decodes plausible noise that STT
    turns into words, and the first symptom is a booking from something nobody
    said. ``build_serializer`` is the single place the rate is applied.
  * The outbound stream handle is ``start.stream_sid`` from the typed field,
    quoted back so the provider routes our audio to the right call. It is never
    taken by re-serializing an inbound frame.

Dependency discipline (the injected-instance trap): the command port that
commits bookings is INJECTED at construction and used as-is per call. The
runtime builds no port of its own and takes no defaulted port — so a test that
counts commits on the port it injected is counting the port the call actually
used, not a different instance the runtime quietly created.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipecat.serializers.exotel import ExotelFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from .call_teardown import OnceRelease
from .open_order import OpenOutcome

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.websockets import WebSocket

    from .frame_pipeline import ResolverContext
    from .media_stream_types import AudioSession, MediaStreamStart
    from .open_order import OpenResult
    from .runtime import CommandPort

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


@dataclass
class VoiceAudioRuntime:
    """Holds the injected dependencies a call needs and owns per-call setup +
    exactly-once teardown.

    The dependencies are set once here, not rebuilt per call and not defaulted:
      * ``command_port`` — the SOLE commit route's port. Injected as an instance;
        the runtime never constructs one, so commit-count assertions observe the
        real object.
      * ``release_slot`` — how to return this call's admission slot. Wrapped in
        an ``OnceRelease`` per call so it runs exactly once across every terminal
        path.
      * ``resolver_factory`` — builds the per-call ``ResolverContext`` for the
        admitted session (business_id, session factory, clock). Injected so tests
        supply a fake and the runtime stays free of DB wiring.
    """

    command_port: CommandPort
    resolver_factory: Callable[[AudioSession, CommandPort], ResolverContext]
    release_slot: Callable[[AudioSession], None]

    def make_release_guard(self, session: AudioSession) -> OnceRelease:
        """One exactly-once release wrapper for this call. The runtime installs
        the returned guard in a single ``finally`` and calls it on every
        terminal path; only the first call returns the slot."""
        return OnceRelease(lambda: self.release_slot(session))

    def build_call_resolver(self, session: AudioSession) -> ResolverContext:
        """Build the per-call resolver via the injected factory, threading the
        INJECTED command port through — never a port built here."""
        return self.resolver_factory(session, self.command_port)

    async def run_call_open(
        self,
        session: AudioSession,
        *,
        open_sequence: Callable[[], Awaitable[OpenResult]],
        start_conversation: Callable[[], Awaitable[None]],
        teardown: Callable[[], Awaitable[None]],
    ) -> OpenResult:
        """Drive the enforced open order, then either start the conversation or
        tear down — with the admission slot released EXACTLY once on every path.

        The compliance ordering lives in ``open_sequence`` (which wraps
        ``open_order.run_open_sequence``): it opens the input latch ONLY after
        notice → playback → evidence succeed. This method's job is the outer
        contract:
          * caller audio never reaches STT unless the open sequence returned
            OPENED — the latch stays closed on any failure, so
            ``start_conversation`` (which lets audio flow) runs only on OPENED;
          * a failed open tears the call down without starting the conversation;
          * the admission slot is released exactly once whether the open
            succeeds, fails, or the whole thing raises — via the OnceRelease
            guard installed in the single ``finally``.

        This is the outer skeleton ``handle_audio_session`` uses; the transport
        and pipeline wiring supply the real ``open_sequence`` /
        ``start_conversation`` / ``teardown`` closures.
        """
        guard = self.make_release_guard(session)
        try:
            result = await open_sequence()
            if result.outcome is OpenOutcome.OPENED:
                await start_conversation()
            else:
                # Open failed: STT never opened (the latch is still closed). Tear
                # down without starting the conversation. The caller already
                # heard the failure line inside the open sequence.
                await teardown()
            return result
        finally:
            # Exactly once, on OPENED, on failure, and on any raise above.
            guard.release()
