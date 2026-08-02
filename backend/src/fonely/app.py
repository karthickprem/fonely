"""Fonely application factory."""

import asyncio
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

    if settings.sarvam_api_key:
        import httpx

        from fonely.services.model_gateway import SarvamModelGateway

        http_client = httpx.AsyncClient()
        app.state.model_gateway = SarvamModelGateway(client=http_client)
    else:
        app.state.model_gateway = None

    try:
        yield
    finally:
        logger.info("shutdown_signal_received")
        try:
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

    if settings.internal_api_secret:
        from fonely.api.internal.appointments import router as appointment_router
        from fonely.api.internal.conversations import router as conversation_router
        from fonely.api.internal.onboarding import router as onboarding_router

        app.include_router(appointment_router)
        app.include_router(conversation_router)
        app.include_router(onboarding_router)

    if settings.whatsapp_verify_token:
        from fonely.api.channels.whatsapp import router as whatsapp_router

        app.include_router(whatsapp_router)

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
