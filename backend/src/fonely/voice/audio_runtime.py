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
    from .input_latch import NoticeInputLatch
    from .media_stream_types import AudioSession, AudioStreamHandoff, MediaStreamStart
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


@dataclass(frozen=True)
class CallComponents:
    """The per-call handles ``handle_audio_session`` drives, produced by the
    ``build_call`` composition step from the trusted session + validated handoff.

    ``input_latch`` starts CLOSED; the open sequence opens it only after notice →
    playback → evidence succeed. ``open_sequence`` is ``run_open_sequence`` bound
    to this call's notice/greeting/evidence/latch. ``teardown`` closes worker,
    providers, and socket. The pipeline/task/runner are the composed Pipecat
    objects (held as ``object`` so this module stays free of runner-type imports
    at module load)."""

    input_latch: NoticeInputLatch
    open_sequence: Callable[[], Awaitable[OpenResult]]
    teardown: Callable[[], Awaitable[None]]
    pipeline_task: object
    runner: object


@dataclass
class VoiceAudioRuntime:
    """Holds the injected dependencies a call needs and owns per-call setup +
    exactly-once teardown.

    The dependencies are set once here, not rebuilt per call and not defaulted:
      * ``command_port_factory`` — builds the SOLE commit route's port PER CALL
        from the admitted ``AudioSession``. It is a FACTORY, not a single port,
        because ``AppointmentServiceCommandPort`` freezes ``business_id`` in its
        ActorContext at construction (backend_ports.py) — a single app-level port
        would bind EVERY call to ONE business, a cross-tenant commit under
        multi-tenant. The factory takes the admitted session so the port's
        business is ``session.business_id`` (the value admission validated and
        wrote the calls row for), NEVER model output or caller data. Cross-tenant
        commit is structurally impossible: the port is bound by construction to
        the session's business.
      * ``release_slot`` — how to return this call's admission slot. Wrapped in
        an ``OnceRelease`` per call so it runs exactly once across every terminal
        path.
      * ``resolver_factory`` — builds the per-call ``ResolverContext`` for the
        admitted session (business_id, session factory, clock), threading the
        per-call command port. Injected so tests supply a fake and the runtime
        stays free of DB wiring.
    """

    command_port_factory: Callable[[AudioSession], CommandPort]
    resolver_factory: Callable[[AudioSession, CommandPort], ResolverContext]
    release_slot: Callable[[AudioSession], None]
    # The concrete composition + runner, injected at CONSTRUCTION (not per call),
    # so ``handle_audio_session`` matches the transport contract
    # ``(websocket, session, handoff)`` exactly while tests still inject fakes —
    # including a FAILING composer/runner — by constructing the runtime with
    # their own ``compose``/``run_runner``. The real mount injects
    # ``runtime_compose.composition_root`` and the real Pipecat runner. Defaulted
    # to None so a runtime built for the resolver/release unit tests (which never
    # call ``handle_audio_session``) needs no composer; calling
    # ``handle_audio_session`` without one raises loudly rather than silently
    # composing nothing.
    compose: Callable[[WebSocket, AudioSession, AudioStreamHandoff], CallComponents] | None = None
    run_runner: Callable[[CallComponents], Awaitable[None]] | None = None

    def make_release_guard(self, session: AudioSession) -> OnceRelease:
        """One exactly-once release wrapper for this call. The runtime installs
        the returned guard in a single ``finally`` and calls it on every
        terminal path; only the first call returns the slot."""
        return OnceRelease(lambda: self.release_slot(session))

    def build_call_command_port(self, session: AudioSession) -> CommandPort:
        """Build the command port for THIS call, bound to the ADMITTED session's
        business. The port's business_id is ``session.business_id`` (validated by
        admission), so a call admitted for business A can only ever commit under
        A — cross-tenant commit is impossible by construction."""
        return self.command_port_factory(session)

    def build_call_resolver(self, session: AudioSession) -> ResolverContext:
        """Build the per-call resolver, threading the per-call command port built
        from the admitted session. The port is session-bound, so the resolver's
        commit route is tenant-isolated by construction."""
        return self.resolver_factory(session, self.build_call_command_port(session))

    async def handle_audio_session(
        self,
        websocket: WebSocket,
        session: AudioSession,
        handoff: AudioStreamHandoff,
    ) -> None:
        """Top-level per-call flow: compose the pipeline for this admitted media
        stream, enforce the open order, run the conversation, converge teardown.

        This is the PRODUCTION contract the telephony adapter calls
        (``exotel.py`` → ``handle_audio_session(websocket, session, handoff)``).
        The concrete composition + runner are injected at CONSTRUCTION
        (``self.compose`` / ``self.run_runner``): ``compose`` builds the transport
        + assembled pipeline + runner from the ``websocket`` and the trusted
        ``session`` + validated ``handoff``, returning the handles this method
        drives; ``run_runner`` drives the Pipecat runner to completion. Both are
        constructor-injected so this signature stays the clean transport contract
        while tests still supply fakes (including a failing composer/runner) by
        constructing the runtime with their own.

        Ordering and teardown are delegated to ``run_call_open``: caller audio
        reaches STT only after the open sequence succeeds, and the admission slot
        releases exactly once on every path. Returns ``None`` — the adapter
        ignores the result; ``run_call_open`` still returns ``OpenResult`` for
        callers that want the outcome.
        """
        if self.compose is None or self.run_runner is None:
            msg = (
                "VoiceAudioRuntime.handle_audio_session requires compose and "
                "run_runner injected at construction"
            )
            raise RuntimeError(msg)
        run_runner = self.run_runner
        # The release guard wraps COMPOSITION too, not just the open sequence:
        # composition builds real providers + a transport and can fail (a
        # provider won't connect, the socket won't open). If that raise escaped
        # before run_call_open installed its own guard, the admission slot would
        # LEAK — the call was admitted but never released. So the slot is
        # released exactly once whether composition raises, the open fails, or
        # the conversation runs to completion.
        guard = self.make_release_guard(session)
        try:
            components = self.compose(websocket, session, handoff)
            await self.run_call_open(
                session,
                open_sequence=components.open_sequence,
                start_conversation=lambda: run_runner(components),
                teardown=components.teardown,
                release_guard=guard,
            )
        finally:
            guard.release()

    async def run_call_open(
        self,
        session: AudioSession,
        *,
        open_sequence: Callable[[], Awaitable[OpenResult]],
        start_conversation: Callable[[], Awaitable[None]],
        teardown: Callable[[], Awaitable[None]],
        release_guard: OnceRelease | None = None,
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
            guard.

        ``release_guard`` lets ``handle_audio_session`` share ONE guard across
        composition + open, so a slot is released exactly once even if
        composition (before this method) raised. When called standalone (its own
        tests), a guard is created here. Either way the guard is idempotent, so a
        shared guard released here and again in the caller's ``finally`` still
        releases the slot exactly once.
        """
        guard = release_guard if release_guard is not None else self.make_release_guard(session)
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
