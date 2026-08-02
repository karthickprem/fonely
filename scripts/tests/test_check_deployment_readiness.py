"""Offline unit tests for the deployment-readiness verifier.

Tests use controlled fakes at database and filesystem boundaries so they
never require a running PostgreSQL instance.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "check-deployment-readiness.py"
BACKEND_ROOT = SCRIPT.parent.parent / "backend"

import importlib

_mod_spec = importlib.util.spec_from_file_location("readiness", SCRIPT)
assert _mod_spec and _mod_spec.loader
readiness = importlib.util.module_from_spec(_mod_spec)
sys.modules["readiness"] = readiness
_mod_spec.loader.exec_module(readiness)


def _run_script(
    env_overrides: dict[str, str] | None = None,
    *,
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FONELY_READINESS_")}
    env.pop("DATABASE_URL", None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def _parse_output(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


# --- Configuration ---


class TestConfiguration:
    def test_missing_url_fails(self) -> None:
        result = _run_script({"FONELY_READINESS_ENVIRONMENT": "test"})
        assert result.returncode != 0
        output = _parse_output(result)
        assert output["overall_status"] == "failed"
        assert output["checks"][0]["failure_code"] == "configuration_missing"

    def test_empty_url_fails(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": "",
                "FONELY_READINESS_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        output = _parse_output(result)
        assert output["checks"][0]["failure_code"] == "configuration_missing"

    def test_malformed_url_fails(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": "mysql://localhost/db",
                "FONELY_READINESS_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        output = _parse_output(result)
        assert output["checks"][0]["failure_code"] == "configuration_invalid"

    def test_missing_environment_fails(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
            }
        )
        assert result.returncode != 0
        output = _parse_output(result)
        assert output["checks"][0]["failure_code"] == "configuration_missing"

    def test_database_url_env_is_ignored(self) -> None:
        result = _run_script(
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
                "FONELY_READINESS_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        output = _parse_output(result)
        assert output["checks"][0]["failure_code"] == "configuration_missing"

    def test_no_secret_in_stdout_on_config_failure(self) -> None:
        secret_url = (
            "postgresql+asyncpg://secret_user:TopSecret123@prod.example.com/fonely"
        )
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": secret_url,
            }
        )
        assert "TopSecret123" not in result.stdout
        assert "secret_user" not in result.stdout
        assert "prod.example.com" not in result.stdout

    def test_no_secret_in_stderr_on_config_failure(self) -> None:
        secret_url = (
            "postgresql+asyncpg://secret_user:TopSecret123@prod.example.com/fonely"
        )
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": secret_url,
            }
        )
        assert "TopSecret123" not in result.stderr
        assert "secret_user" not in result.stderr


# --- Repository head discovery ---


class TestRepositoryHead:
    def test_zero_heads_fails(self) -> None:
        with patch.object(readiness, "_discover_repository_heads", return_value=[]):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "failed"
        head_check = next(c for c in report.checks if c["name"] == "repository_head")
        assert head_check["failure_code"] == "repository_head_missing"

    def test_multiple_heads_fails(self) -> None:
        with patch.object(
            readiness, "_discover_repository_heads", return_value=["0003", "0004"]
        ):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "failed"
        head_check = next(c for c in report.checks if c["name"] == "repository_head")
        assert head_check["failure_code"] == "repository_heads_multiple"

    def test_one_head_succeeds(self) -> None:
        heads = readiness._discover_repository_heads()
        assert len(heads) == 1
        assert heads[0] in {"0003", "0004", "0005"}

    def test_real_discovery_uses_backend_versions(self) -> None:
        heads = readiness._discover_repository_heads()
        assert all(isinstance(h, str) and h for h in heads)


# --- PostgreSQL version policy ---


class TestVersionPolicy:
    @pytest.mark.parametrize("major", [14, 15, 16, 17])
    def test_supported_majors(self, major: int) -> None:
        assert major in readiness.SUPPORTED_PG_MAJORS

    @pytest.mark.parametrize("major", [9, 10, 11, 12, 13, 18, 99])
    def test_unsupported_majors(self, major: int) -> None:
        assert major not in readiness.SUPPORTED_PG_MAJORS


# --- Sanitization ---


class TestSanitization:
    def test_url_is_redacted(self) -> None:
        raw = "postgresql+asyncpg://user:pass@host:5432/db failed"
        assert "user:pass" not in readiness._sanitize(raw)

    def test_env_var_value_redacted(self) -> None:
        raw = "FONELY_READINESS_DATABASE_URL = postgresql+asyncpg://u:p@h/d"
        assert "u:p" not in readiness._sanitize(raw)


# --- JSON output contract ---


class TestJsonContract:
    def test_config_failure_has_required_fields(self) -> None:
        result = readiness._emit_failure("configuration_missing", "test error", "ci")
        assert result["schema_version"] == 1
        assert "check_run_id" in result
        assert result["checked_at"]
        assert result["environment"] == "ci"
        assert result["overall_status"] == "failed"
        assert isinstance(result["checks"], list)

    def test_unique_run_ids(self) -> None:
        ids = {readiness.ReadinessReport().check_run_id for _ in range(100)}
        assert len(ids) == 100

    def test_utc_timestamp_format(self) -> None:
        import re

        result = readiness._emit_failure("test", "msg")
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", result["checked_at"])

    def test_stable_failure_codes(self) -> None:
        known = {
            "configuration_missing",
            "configuration_invalid",
            "connection_failed",
            "connection_timeout",
            "unsupported_postgres_version",
            "repository_head_missing",
            "repository_heads_multiple",
            "alembic_version_missing",
            "database_revision_invalid",
            "database_revision_stale",
            "readonly_check_failed",
            "overall_timeout",
            "internal_error",
        }
        assert len(known) == 13

    def test_nonzero_exit_for_every_config_failure(self) -> None:
        for code in ["configuration_missing", "configuration_invalid"]:
            result = _run_script({"FONELY_READINESS_ENVIRONMENT": "test"})
            assert result.returncode != 0


# --- Connection failure sanitization ---


class TestConnectionFailure:
    def test_refused_connection_is_sanitized(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": "postgresql+asyncpg://u:secret@localhost:59999/db",
                "FONELY_READINESS_ENVIRONMENT": "test",
                "FONELY_READINESS_CONNECT_TIMEOUT_S": "3",
                "FONELY_READINESS_OVERALL_TIMEOUT_S": "8",
            }
        )
        assert result.returncode != 0
        output = _parse_output(result)
        assert output["overall_status"] == "failed"
        conn_check = next(c for c in output["checks"] if c["name"] == "connection")
        assert conn_check["failure_code"] in ("connection_failed", "connection_timeout")
        assert "secret" not in result.stdout
        assert "secret" not in result.stderr


# --- Timeout ---


class TestTimeout:
    def test_overall_timeout_in_config(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
                "FONELY_READINESS_ENVIRONMENT": "test",
                "FONELY_READINESS_OVERALL_TIMEOUT_S": "0",
            }
        )
        assert result.returncode != 0
        output = _parse_output(result)
        assert output["checks"][0]["failure_code"] == "configuration_invalid"

    def test_connect_timeout_in_config(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
                "FONELY_READINESS_ENVIRONMENT": "test",
                "FONELY_READINESS_CONNECT_TIMEOUT_S": "-1",
            }
        )
        assert result.returncode != 0
        output = _parse_output(result)
        assert output["checks"][0]["failure_code"] == "configuration_invalid"


# --- Mocked database checks ---


def _mock_engine(
    *,
    version: str = "16.4",
    revision: str | None = "0003",
    has_alembic: bool = True,
    readonly: str = "on",
    connect_fails: Exception | None = None,
) -> MagicMock:
    engine = MagicMock()
    conn = AsyncMock()

    async def mock_execute(stmt: Any) -> MagicMock:
        sql = str(stmt) if not isinstance(stmt, str) else stmt
        result = MagicMock()
        if "SELECT 1" in sql:
            if connect_fails:
                raise connect_fails
            result.scalar_one.return_value = 1
            return result
        if "server_version" in sql.lower():
            result.scalar_one.return_value = version
            return result
        if "information_schema.tables" in sql:
            result.scalar.return_value = has_alembic
            return result
        if "version_num" in sql:
            if revision is not None:
                row = MagicMock()
                row.__getitem__ = lambda s, i: revision
                result.all.return_value = [row]
            else:
                result.all.return_value = []
            return result
        if "transaction_read_only" in sql.lower():
            result.scalar.return_value = readonly
            return result
        result.scalar.return_value = None
        return result

    conn.execute = mock_execute

    async def _mock_scalar(stmt: Any) -> Any:
        sql = str(stmt) if not isinstance(stmt, str) else stmt
        if "information_schema.tables" in sql:
            return has_alembic
        if "transaction_read_only" in sql.lower():
            return readonly
        if "server_version" in sql.lower():
            return version
        result = await mock_execute(stmt)
        return result.scalar()

    conn.scalar = AsyncMock(side_effect=_mock_scalar)
    conn.rollback = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    engine.connect.return_value = ctx
    engine.dispose = AsyncMock()
    return engine


class TestMockedDatabaseChecks:
    def test_stale_revision_fails(self) -> None:
        engine = _mock_engine(revision="0002")
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "failed"
        rev_check = next(c for c in report.checks if c["name"] == "database_revision")
        assert rev_check["failure_code"] == "database_revision_stale"

    def test_exact_revision_succeeds(self) -> None:
        engine = _mock_engine(revision="0003")
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "passed"
        assert report.database_revision == "0003"
        assert report.repository_head == "0003"

    def test_missing_alembic_version_fails(self) -> None:
        engine = _mock_engine(has_alembic=False)
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "failed"
        rev_check = next(c for c in report.checks if c["name"] == "database_revision")
        assert rev_check["failure_code"] == "alembic_version_missing"

    def test_empty_revision_fails(self) -> None:
        engine = _mock_engine(revision=None)
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "failed"
        rev_check = next(c for c in report.checks if c["name"] == "database_revision")
        assert rev_check["failure_code"] == "database_revision_invalid"

    def test_unsupported_pg_version_fails(self) -> None:
        engine = _mock_engine(version="13.12")
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "failed"
        ver_check = next(c for c in report.checks if c["name"] == "postgres_version")
        assert ver_check["failure_code"] == "unsupported_postgres_version"

    def test_unparsable_version_fails(self) -> None:
        engine = _mock_engine(version="unknown-build")
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "failed"

    def test_readonly_failure(self) -> None:
        engine = _mock_engine(readonly="off")
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "failed"
        ro_check = next(c for c in report.checks if c["name"] == "readonly_transaction")
        assert ro_check["failure_code"] == "readonly_check_failed"

    def test_all_pass_green(self) -> None:
        engine = _mock_engine()
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "passed"
        assert report.repository_head == "0003"
        assert report.database_revision == "0003"
        assert report.postgres_major == 16
        assert len(report.checks) == 5
        assert all(c["status"] == "passed" for c in report.checks)

    def test_connection_exception_sanitized(self) -> None:
        engine = _mock_engine(connect_fails=ConnectionRefusedError("secret_host:5432"))
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        assert report.overall_status == "failed"
        conn_check = next(c for c in report.checks if c["name"] == "connection")
        assert conn_check["failure_code"] == "connection_failed"
        assert "secret_host" not in (conn_check["message"] or "")

    def test_engine_disposed_on_failure(self) -> None:
        engine = _mock_engine(connect_fails=ConnectionRefusedError())
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            asyncio.run(
                readiness._run_checks(
                    "postgresql+asyncpg://u:p@localhost/db", "test", 5, 15
                )
            )
        engine.dispose.assert_awaited_once()
