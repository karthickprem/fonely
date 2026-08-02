"""Tests for staging deployment readiness: app factory, pool config, logging."""

import json
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from fonely.core.config import Settings
from fonely.core.logging_config import JsonFormatter, configure_logging


class TestAppFactory:
    def test_create_app_returns_fastapi(self) -> None:
        from fonely.app import create_app

        app = create_app()
        assert app.title == "Fonely Internal API"

    def test_health_live_route_exists(self) -> None:
        from fonely.app import create_app

        app = create_app()
        routes = {r.path for r in app.routes}
        assert "/health/live" in routes

    def test_health_ready_route_exists(self) -> None:
        from fonely.app import create_app

        app = create_app()
        routes = {r.path for r in app.routes}
        assert "/health/ready" in routes


class TestPoolConfiguration:
    def test_default_pool_settings(self) -> None:
        s = Settings()
        assert s.db_pool_size == 5
        assert s.db_max_overflow == 5
        assert s.db_pool_timeout == 30
        assert s.db_pool_recycle == 1800

    def test_custom_pool_from_env(self) -> None:
        env = {
            "DB_POOL_SIZE": "10",
            "DB_MAX_OVERFLOW": "3",
            "DB_POOL_TIMEOUT": "15",
            "DB_POOL_RECYCLE": "900",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.db_pool_size == 10
        assert s.db_max_overflow == 3
        assert s.db_pool_timeout == 15
        assert s.db_pool_recycle == 900


class TestStructuredLogging:
    def test_json_formatter_produces_valid_json(self) -> None:
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello",
            args=(),
            exc_info=None,
        )
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "hello"
        assert "timestamp" in parsed

    def test_json_formatter_includes_correlation_id(self) -> None:
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        record.correlation_id = "abc-123"  # type: ignore[attr-defined]
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["correlation_id"] == "abc-123"

    def test_configure_logging_json(self) -> None:
        configure_logging("json", "DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)

    def test_configure_logging_text(self) -> None:
        configure_logging("text", "WARNING")
        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_default_log_settings(self) -> None:
        s = Settings()
        assert s.log_format == "text"
        assert s.log_level == "INFO"


class TestMigrationRunner:
    def test_script_exists_and_executable(self) -> None:
        script = Path(__file__).parents[2] / "scripts" / "run-migrations.sh"
        assert script.exists()
        assert os.access(script, os.X_OK)

    def test_script_syntax(self) -> None:
        script = Path(__file__).parents[2] / "scripts" / "run-migrations.sh"
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0


class TestDockerConfiguration:
    def test_dockerfile_exists(self) -> None:
        dockerfile = Path(__file__).parent.parent / "Dockerfile"
        assert dockerfile.exists()

    def test_dockerfile_uses_nonroot_user(self) -> None:
        dockerfile = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile.read_text()
        assert "USER fonely" in content

    def test_dockerfile_no_env_baked(self) -> None:
        dockerfile = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile.read_text()
        assert ".env" not in content or ".dockerignore" in content

    def test_dockerignore_excludes_sensitive(self) -> None:
        ignore = Path(__file__).parent.parent / ".dockerignore"
        assert ignore.exists()
        content = ignore.read_text()
        assert ".env" in content
        assert "tests/" in content

    def test_compose_staging_exists(self) -> None:
        compose = Path(__file__).parents[2] / "docker-compose.staging.yml"
        assert compose.exists()

    def test_env_template_no_real_secrets(self) -> None:
        template = Path(__file__).parents[2] / ".env.staging.template"
        assert template.exists()
        content = template.read_text()
        assert "changeme" in content
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                _, _, val = line.partition("=")
                assert "prod" not in val.lower() or "changeme" in val.lower()
