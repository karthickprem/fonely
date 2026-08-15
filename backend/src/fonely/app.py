"""Fonely application factory."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.core.logging_config import configure_logging
from fonely.core.middleware import apply_hardening

logger = logging.getLogger("fonely.app")


def _build_voice_audio_runtime(app: FastAPI) -> object:
    """Construct the ONE canonical VoiceAudioRuntime for this process.

    Called only when ``voice_pipeline_enabled`` is on. The command port is a
    PER-CALL factory, never a single instance: ``AppointmentServiceCommandPort``
    freezes ``business_id`` at construction, so a single port would bind every
    call to one business (cross-tenant commit). The factory builds the port from
    each admitted session's identity — ``session.business_id`` (validated by
    admission), so a call admitted for business A can only ever commit under A.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from fonely.api.internal.validation import InternalValidationPort
    from fonely.domain.appointments.validation import AppointmentValidationPort
    from fonely.voice.audio_runtime import VoiceAudioRuntime
    from fonely.voice.backend_ports import (
        AppointmentServiceCommandPort,
        build_actor_context,
    )
    from fonely.voice.context import TrustedClock
    from fonely.voice.frame_pipeline import ResolverContext
    from fonely.voice.media_stream_types import AudioSession
    from fonely.voice.runtime import CommandPort
    from fonely.voice.runtime_compose import make_composition_root, run_pipeline_runner

    session_factory = app.state.session_factory

    def _validation_factory(db: AsyncSession) -> AppointmentValidationPort:
        return InternalValidationPort(db)

    def command_port_factory(admitted: AudioSession) -> CommandPort:
        # Business / caller / call identity ALL come from the admitted session,
        # never model output or caller-supplied data. The port is bound to
        # admitted.business_id by construction.
        actor = build_actor_context(
            business_id=admitted.business_id,
            phone=admitted.caller_phone or "",
            session_id=str(admitted.call_id),
        )
        return AppointmentServiceCommandPort(
            actor=actor,
            session_factory=session_factory,
            validation_factory=_validation_factory,
            business_timezone=admitted.timezone,
            conversation_id=str(admitted.call_id),
            # Trusted provenance: the appointment links back to THIS admitted call
            # via the (business_id, call_id) FK. call_id comes only from the
            # admitted session, never model/caller data.
            call_id=admitted.call_id,
        )

    def resolver_factory(admitted: AudioSession, command_port: CommandPort) -> ResolverContext:
        return ResolverContext(
            business_id=admitted.business_id,
            session_factory=session_factory,
            command_port=command_port,
            clock=TrustedClock.from_now(admitted.timezone),
            # Trusted call id → the gate composes a restart-stable idempotency key.
            call_id=admitted.call_id,
        )

    def release_slot(admitted: AudioSession) -> None:
        # Admission-slot release is the admission lane's concern; the runtime
        # only needs a callable. With no admission-slot accounting wired here yet
        # this is a no-op — the OnceRelease guard still proves exactly-once
        # semantics, and real slot accounting plugs in when the admission lane
        # exposes it. (NOT RUN: real slot accounting is admission-lane work.)
        return None

    runtime = VoiceAudioRuntime(
        command_port_factory=command_port_factory,
        resolver_factory=resolver_factory,
        release_slot=release_slot,
        run_runner=run_pipeline_runner,
    )
    runtime.compose = make_composition_root(
        runtime,
        session_factory=session_factory,
        system_prompt=_voice_system_prompt(),
    )
    return runtime


def _voice_system_prompt() -> str:
    """The system prompt seeding the voice LLM context. Kept minimal here; the
    BookingStateInjector rewrites the context per turn with live clinic facts."""
    return (
        "You are Fonely, an automated appointment-booking assistant for a dental "
        "clinic. Speak the caller's language. Book only from confirmed "
        "availability. Never give medical advice."
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging(settings.log_format, settings.log_level)
    logger.info("starting", extra={"host": settings.host, "port": settings.port})
    engine: AsyncEngine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
    )
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    http_client = None
    if settings.sarvam_api_key:
        import httpx

        from fonely.services.model_gateway import SarvamModelGateway

        http_client = httpx.AsyncClient()
        app.state.http_client = http_client
        app.state.model_gateway = SarvamModelGateway(client=http_client)
    else:
        app.state.model_gateway = None

    # The canonical voice runtime is mounted ONLY when voice_pipeline_enabled is
    # on. Default OFF: exotel.py refuses an unmounted runtime with a clean 1011
    # (absence must not read as success), so shipping dark is safe and is the
    # default until a hosted exact-SHA call proves the path.
    #
    # FAIL CLOSED when enabled: the composition builds providers PER CALL
    # (lazily), so a voice-enabled process with missing/misconfigured
    # SELECTED-provider credentials would construct a runtime that looks fine at
    # startup and fails on the FIRST REAL CALL — silently serving dead calls.
    # Gate on the ACTUALLY-SELECTED providers (resolved_voice_creds_ready, NOT
    # the hardcoded credentials_ready snapshot) and RAISE if not serviceable: a
    # raise here, before yield, means the app FAILS TO START — no silent-serving
    # process. This whole block is INSIDE the flag branch, so a flag-OFF process
    # is byte-for-byte unaffected (default-dark stays dark).
    if settings.voice_pipeline_enabled:
        from fonely.voice.config import LLMConfig, STTConfig, TTSConfig
        from fonely.voice.providers import resolved_voice_creds_ready

        resolved_voice_creds_ready(STTConfig(), TTSConfig(), LLMConfig())
        runtime = _build_voice_audio_runtime(app)
        if runtime is None:
            raise RuntimeError(
                "voice_pipeline_enabled but the voice runtime is None — refusing "
                "to start and serve silent calls"
            )
        app.state.voice_audio_runtime = runtime
    else:
        app.state.voice_audio_runtime = None

    try:
        yield
    finally:
        logger.info("shutdown_signal_received")
        try:
            if http_client is not None:
                await http_client.aclose()
            await engine.dispose()
            logger.info("shutdown_complete")
        except Exception:
            logger.error("engine_disposal_failed", extra={"operation": "shutdown"})


def create_app() -> FastAPI:
    app = FastAPI(
        title="Fonely Internal API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next: object) -> Response:
        correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
        request.state.correlation_id = correlation_id
        response: Response = await call_next(request)  # type: ignore[operator]
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    apply_hardening(app)

    # Each router group is gated on the credential it cannot work without.
    # An unset credential used to mean the routes simply were not there, and
    # callers got a 404 that looks exactly like a wrong URL -- an operator can
    # spend an afternoon on that. Say at startup which groups are off and
    # which setting turns them on, so a missing capability is visible rather
    # than merely absent.
    disabled: list[str] = []

    if settings.internal_api_secret:
        from fonely.api.internal.appointments import router as appointment_router
        from fonely.api.internal.conversations import router as conversation_router
        from fonely.api.internal.onboarding import router as onboarding_router

        app.include_router(appointment_router)
        app.include_router(conversation_router)
        app.include_router(onboarding_router)
    else:
        disabled.append("internal API (booking, conversations, onboarding): INTERNAL_API_SECRET")

    if settings.whatsapp_verify_token:
        from fonely.api.channels.whatsapp import router as whatsapp_router

        app.include_router(whatsapp_router)
    else:
        disabled.append("WhatsApp channel: WHATSAPP_VERIFY_TOKEN")

    if settings.exotel_webhook_secret:
        # No number mapping is loaded here any more. Which clinic a dialed
        # number reaches is read per request from business_channel_identities
        # (migration 0017), so attaching a number is an API call rather than a
        # redeploy.
        from fonely.api.channels.exotel import router as exotel_router

        app.include_router(exotel_router)
    else:
        disabled.append("Exotel voice channel: EXOTEL_WEBHOOK_SECRET")

    if disabled:
        logger.warning("router_groups_disabled", extra={"missing_settings": disabled})
        for entry in disabled:
            logger.warning("router_group_disabled: %s is not set", entry)

    @app.get("/metrics")
    async def metrics_endpoint(request: Request) -> Response:
        from fonely.core.metrics import metrics

        data = metrics.export()
        return Response(
            content=json.dumps(data, indent=2),
            media_type="application/json",
        )

    @app.get("/health/alerts")
    async def alerts_endpoint(request: Request) -> Response:
        from fonely.core.alerts import check_alerts
        from fonely.core.metrics import metrics

        alerts = check_alerts(metrics)
        status_code = 200 if not alerts else 503
        return Response(
            content=json.dumps({"alerts": alerts}),
            media_type="application/json",
            status_code=status_code,
        )

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness(request: Request) -> Response:
        timeout = settings.readiness_timeout_seconds
        engine: AsyncEngine = request.app.state.engine
        try:
            async with asyncio.timeout(timeout):
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            # Defense-in-depth for config drift AFTER a healthy start (creds
            # rotated/revoked while running): a voice-ENABLED process must not
            # report ready unless it can still serve calls — a mounted runtime
            # AND the selected providers still configured. Startup fail-closed
            # (lifespan) covers a broken deploy never starting; this covers a
            # once-healthy process going unserviceable. Flag-OFF processes skip
            # this entirely (default-dark stays ready on the DB check alone).
            if settings.voice_pipeline_enabled:
                from fonely.voice.config import LLMConfig, STTConfig, TTSConfig
                from fonely.voice.providers import resolved_voice_creds_ready

                if getattr(request.app.state, "voice_audio_runtime", None) is None:
                    raise RuntimeError("voice enabled but runtime not mounted")
                resolved_voice_creds_ready(STTConfig(), TTSConfig(), LLMConfig())
            return Response(
                content='{"status":"ready"}',
                media_type="application/json",
                status_code=200,
            )
        except (TimeoutError, Exception):
            return Response(
                content='{"status":"unavailable"}',
                media_type="application/json",
                status_code=503,
            )

    return app
