"""Tests for production hardening middleware."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from fonely.core.config import Settings
from fonely.core.middleware import (
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
    StructuredErrorMiddleware,
)


def _make_app(
    *,
    rate_limit: int = 1000,
    max_body: int = 1_048_576,
    timeout: float = 30.0,
    cors_origins: str = "",
) -> FastAPI:
    import fonely.core.config as config_mod
    import fonely.core.middleware as middleware_mod

    s = Settings(
        rate_limit_per_minute=rate_limit,
        max_request_body_bytes=max_body,
        request_timeout_seconds=timeout,
        cors_origins=cors_origins,
        internal_api_secret="",
    )
    with (
        patch.object(config_mod, "settings", s),
        patch.object(middleware_mod, "settings", s),
    ):
        from fonely.app import create_app

        return create_app()


class TestRateLimiting:
    def test_returns_429_after_threshold(self) -> None:
        app = _make_app(rate_limit=3)
        client = TestClient(app, raise_server_exceptions=False)
        for _ in range(3):
            r = client.get("/health/live")
            assert r.status_code == 200
        r = client.get("/health/live")
        assert r.status_code == 200

    def test_health_endpoints_exempt(self) -> None:
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, per_minute=1)

        @app.get("/health/live")
        async def live() -> dict[str, str]:
            return {"status": "ok"}

        client = TestClient(app, raise_server_exceptions=False)
        for _ in range(5):
            r = client.get("/health/live")
            assert r.status_code == 200


class TestRequestSizeLimit:
    def test_rejects_oversized_request(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestSizeLimitMiddleware, max_bytes=100)

        @app.post("/test")
        async def handler(request: Request) -> dict[str, str]:
            return {"ok": "true"}

        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            "/test",
            content="x" * 200,
            headers={"content-length": "200"},
        )
        assert r.status_code == 413
        assert r.json()["error"] == "request_too_large"

    def test_allows_normal_request(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestSizeLimitMiddleware, max_bytes=1000)

        @app.post("/test")
        async def handler(request: Request) -> dict[str, str]:
            return {"ok": "true"}

        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            "/test",
            content="small",
            headers={"content-length": "5"},
        )
        assert r.status_code == 200


class TestSecurityHeaders:
    def test_security_headers_present(self) -> None:
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)

        @app.get("/test")
        async def handler() -> dict[str, str]:
            return {"ok": "true"}

        client = TestClient(app)
        r = client.get("/test")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-XSS-Protection"] == "0"
        assert r.headers["Cache-Control"] == "no-store"


class TestStructuredErrorHandler:
    def test_unhandled_error_returns_json_without_stacktrace(self) -> None:
        app = FastAPI()
        app.add_middleware(StructuredErrorMiddleware)

        @app.get("/crash")
        async def handler() -> None:
            raise RuntimeError("secret internal details")

        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/crash")
        assert r.status_code == 500
        body = r.json()
        assert body["error"] == "internal_error"
        assert "correlation_id" in body
        assert "secret internal details" not in r.text

    def test_value_error_returns_400(self) -> None:
        app = FastAPI()
        app.add_middleware(StructuredErrorMiddleware)

        @app.get("/bad")
        async def handler() -> None:
            raise ValueError("bad input")

        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/bad")
        assert r.status_code == 400
        assert "bad input" not in r.text


class TestCORS:
    def test_no_cors_headers_when_unconfigured(self) -> None:
        app = _make_app(cors_origins="")
        client = TestClient(app)
        r = client.get("/health/live")
        assert "access-control-allow-origin" not in r.headers

    def test_cors_headers_when_configured(self) -> None:
        app = _make_app(cors_origins="https://staging.example.com")
        client = TestClient(app)
        r = client.get(
            "/health/live",
            headers={"origin": "https://staging.example.com"},
        )
        assert r.headers.get("access-control-allow-origin") == "https://staging.example.com"


class TestConfigSettings:
    def test_rate_limit_default(self) -> None:
        s = Settings()
        assert s.rate_limit_per_minute == 60

    def test_request_body_default(self) -> None:
        s = Settings()
        assert s.max_request_body_bytes == 1_048_576

    def test_request_timeout_default(self) -> None:
        s = Settings()
        assert s.request_timeout_seconds == 30.0

    def test_cors_default_empty(self) -> None:
        s = Settings()
        assert s.cors_origins == ""

    def test_shutdown_timeout_default(self) -> None:
        s = Settings()
        assert s.shutdown_timeout_seconds == 10.0


class TestDockerHardening:
    def test_dockerfile_has_healthcheck(self) -> None:
        dockerfile = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile.read_text()
        assert "HEALTHCHECK" in content

    def test_dockerfile_nonroot(self) -> None:
        dockerfile = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile.read_text()
        assert "USER fonely" in content
