"""Production hardening middleware for Fonely."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any, ClassVar

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware

from fonely.core.config import settings

logger = logging.getLogger("fonely.middleware")

_HEALTH_PATHS = frozenset({"/health/live", "/health/ready"})


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, per_minute: int = 60) -> None:
        super().__init__(app)
        self._per_minute = per_minute
        self._requests: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _HEALTH_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window_start = now - 60.0

        timestamps = self._requests[client_ip]
        self._requests[client_ip] = [t for t in timestamps if t > window_start]

        if len(self._requests[client_ip]) >= self._per_minute:
            return Response(
                content=json.dumps({"error": "rate_limit_exceeded"}),
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": "60"},
            )

        self._requests[client_ip].append(now)

        if len(self._requests) > 1000:
            stale_ips = [ip for ip, ts in self._requests.items() if not ts or ts[-1] < window_start]
            for ip in stale_ips:
                del self._requests[ip]

        return await call_next(request)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, max_bytes: int = 1_048_576) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self._max_bytes:
                    return Response(
                        content=json.dumps({"error": "request_too_large"}),
                        status_code=413,
                        media_type="application/json",
                    )
            except (ValueError, OverflowError):
                return Response(
                    content=json.dumps({"error": "invalid_content_length"}),
                    status_code=400,
                    media_type="application/json",
                )
            return await call_next(request)
        return await call_next(request)


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, timeout_seconds: float = 30.0) -> None:
        super().__init__(app)
        self._timeout = timeout_seconds

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await asyncio.wait_for(call_next(request), timeout=self._timeout)
        except TimeoutError:
            correlation_id = getattr(request.state, "correlation_id", "unknown")
            logger.warning(
                "request_timeout",
                extra={"path": request.url.path, "correlation_id": correlation_id},
            )
            return Response(
                content=json.dumps({"error": "gateway_timeout"}),
                status_code=504,
                media_type="application/json",
            )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Cache-Control"] = "no-store"
        host = request.headers.get("host", "")
        if "localhost" not in host and "127.0.0.1" not in host:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if "server" in response.headers:
            del response.headers["server"]
        return response


class StructuredErrorMiddleware(BaseHTTPMiddleware):
    _STATUS_MAP: ClassVar[dict[str, int]] = {
        "ValueError": 400,
        "PendingActionNotFoundError": 404,
        "PendingActionUnauthorizedError": 403,
        "PendingActionConcurrencyError": 409,
        "PendingActionExpiredError": 410,
    }

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            correlation_id = getattr(request.state, "correlation_id", "unknown")
            exc_name = type(exc).__name__
            status = self._STATUS_MAP.get(exc_name, 500)
            logger.error(
                "unhandled_exception",
                extra={
                    "exception_type": exc_name,
                    "correlation_id": correlation_id,
                    "path": request.url.path,
                },
                exc_info=True,
            )
            return Response(
                content=json.dumps({"error": "internal_error", "correlation_id": correlation_id}),
                status_code=status,
                media_type="application/json",
            )


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _HEALTH_PATHS or request.url.path == "/metrics":
            return await call_next(request)

        from fonely.core.metrics import metrics, normalize_path

        path = normalize_path(request.url.path)
        metrics.increment_gauge("http_requests_active")
        start = time.monotonic()
        try:
            response = await call_next(request)
            status = str(response.status_code)
        except Exception:
            status = "500"
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.increment(
                "http_requests_total",
                {"method": request.method, "path": path, "status": status},
            )
            metrics.observe("http_request_duration_ms", duration_ms, {"path": path})
            metrics.decrement_gauge("http_requests_active")
        return response


def apply_hardening(app: FastAPI) -> None:
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(StructuredErrorMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        RequestTimeoutMiddleware,
        timeout_seconds=settings.request_timeout_seconds,
    )
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=settings.max_request_body_bytes,
    )
    app.add_middleware(
        RateLimitMiddleware,
        per_minute=settings.rate_limit_per_minute,
    )
    if settings.cors_origins:
        origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
        if origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
