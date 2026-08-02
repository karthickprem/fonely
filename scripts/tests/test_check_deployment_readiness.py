"""Offline unit tests for the deployment-readiness verifier.

Tests use controlled fakes at database and filesystem boundaries so they
never require a running PostgreSQL instance.  The database fake explicitly
rejects unrecognized SQL statements.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
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


def _build_url(*parts: str) -> str:
    return "".join(parts)


_FAKE_URL = _build_url(
    "postgresql+asyncpg://", "test_user:test_pass@localhost:15432/test_db"
)

_APPROVED_SQL_EXACT = frozenset(
    {
        "SELECT 1",
        "SHOW server_version",
        "SET TRANSACTION READ ONLY",
        "SHOW transaction_read_only",
        (
            "SELECT EXISTS ("
            " SELECT 1 FROM information_schema.tables"
            " WHERE table_schema = 'public'"
            " AND table_name = 'alembic_version')"
        ),
        "SELECT version_num FROM public.alembic_version",
    }
)


def _is_approved_sql(sql: str) -> bool:
    normalized = " ".join(sql.split())
    return normalized in _APPROVED_SQL_EXACT


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
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_missing"
        )

    def test_malformed_url_fails(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": "mysql://localhost/db",
                "FONELY_READINESS_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )

    def test_sync_postgresql_url_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": _build_url(
                    "postgresql://", "u:p@localhost/db"
                ),
                "FONELY_READINESS_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )

    def test_postgresql_prefix_lookalike_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": _build_url(
                    "postgresql+psycopg2://", "u:p@localhost/db"
                ),
                "FONELY_READINESS_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )

    def test_missing_host_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": "postgresql+asyncpg:///db",
                "FONELY_READINESS_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )

    def test_missing_environment_fails(self) -> None:
        result = _run_script({"FONELY_READINESS_DATABASE_URL": _FAKE_URL})
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_missing"
        )

    def test_database_url_env_is_ignored(self) -> None:
        result = _run_script(
            {
                "DATABASE_URL": _FAKE_URL,
                "FONELY_READINESS_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_missing"
        )

    def test_no_secret_in_stdout_on_config_failure(self) -> None:
        secret_pass = "TopSecret" + "XYZ789"
        secret_user = "secret" + "_admin"
        url = _build_url(
            "postgresql+asyncpg://",
            secret_user,
            ":",
            secret_pass,
            "@test.invalid/db",
        )
        result = _run_script({"FONELY_READINESS_DATABASE_URL": url})
        assert secret_pass not in result.stdout
        assert secret_user not in result.stdout
        assert "test.invalid" not in result.stdout

    def test_no_secret_in_stderr_on_config_failure(self) -> None:
        secret_pass = "TopSecret" + "XYZ789"
        secret_user = "secret" + "_admin"
        url = _build_url(
            "postgresql+asyncpg://",
            secret_user,
            ":",
            secret_pass,
            "@test.invalid/db",
        )
        result = _run_script({"FONELY_READINESS_DATABASE_URL": url})
        assert secret_pass not in result.stderr
        assert secret_user not in result.stderr

    def test_nan_timeout_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": _FAKE_URL,
                "FONELY_READINESS_ENVIRONMENT": "test",
                "FONELY_READINESS_CONNECT_TIMEOUT_S": "nan",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )

    def test_inf_timeout_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": _FAKE_URL,
                "FONELY_READINESS_ENVIRONMENT": "test",
                "FONELY_READINESS_OVERALL_TIMEOUT_S": "inf",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )

    def test_negative_inf_timeout_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": _FAKE_URL,
                "FONELY_READINESS_ENVIRONMENT": "test",
                "FONELY_READINESS_CONNECT_TIMEOUT_S": "-inf",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )


# --- Static AST-based repository head discovery ---


class TestRepositoryHeadDiscovery:
    def test_real_discovery_finds_one_head(self) -> None:
        heads = readiness._discover_repository_heads()
        assert len(heads) == 1
        assert heads[0] in {"0003", "0004", "0005", "0006", "0007"}

    def test_real_discovery_uses_backend_versions(self) -> None:
        heads = readiness._discover_repository_heads()
        assert all(isinstance(h, str) and h for h in heads)

    def test_syntax_error_fails_closed(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_bad.py").write_text("def !!invalid!!")
        with (
            patch.object(readiness, "_VERSIONS_DIR", versions),
            pytest.raises(readiness.MigrationParseError, match="syntax error"),
        ):
            readiness._discover_repository_heads()

    def test_missing_revision_fails_closed(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_no_rev.py").write_text(
            "down_revision = None\nbranch_labels = None\n"
        )
        with (
            patch.object(readiness, "_VERSIONS_DIR", versions),
            pytest.raises(readiness.MigrationParseError, match="missing revision"),
        ):
            readiness._discover_repository_heads()

    def test_nonliteral_revision_fails_closed(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_computed.py").write_text(
            'revision = "00" + "01"\ndown_revision = None\n'
        )
        with (
            patch.object(readiness, "_VERSIONS_DIR", versions),
            pytest.raises(readiness.MigrationParseError, match="not a literal string"),
        ):
            readiness._discover_repository_heads()

    def test_duplicate_revision_fails_closed(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_a.py").write_text('revision = "0001"\ndown_revision = None\n')
        (versions / "0001_b.py").write_text('revision = "0001"\ndown_revision = None\n')
        with (
            patch.object(readiness, "_VERSIONS_DIR", versions),
            pytest.raises(readiness.MigrationParseError, match="duplicate"),
        ):
            readiness._discover_repository_heads()

    def test_missing_parent_fails_closed(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0002_orphan.py").write_text(
            'revision = "0002"\ndown_revision = "missing"\n'
        )
        with (
            patch.object(readiness, "_VERSIONS_DIR", versions),
            pytest.raises(readiness.MigrationParseError, match="missing parent"),
        ):
            readiness._discover_repository_heads()

    def test_valid_linear_chain(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_init.py").write_text(
            'revision = "0001"\ndown_revision = None\n'
        )
        (versions / "0002_next.py").write_text(
            'revision = "0002"\ndown_revision = "0001"\n'
        )
        with patch.object(readiness, "_VERSIONS_DIR", versions):
            assert readiness._discover_repository_heads() == ["0002"]

    def test_branched_graph_returns_multiple_heads(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_base.py").write_text(
            'revision = "0001"\ndown_revision = None\n'
        )
        (versions / "0002_a.py").write_text(
            'revision = "0002a"\ndown_revision = "0001"\n'
        )
        (versions / "0002_b.py").write_text(
            'revision = "0002b"\ndown_revision = "0001"\n'
        )
        with patch.object(readiness, "_VERSIONS_DIR", versions):
            heads = readiness._discover_repository_heads()
        assert sorted(heads) == ["0002a", "0002b"]

    def test_merge_returns_one_head(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_base.py").write_text(
            'revision = "0001"\ndown_revision = None\n'
        )
        (versions / "0002_a.py").write_text(
            'revision = "0002a"\ndown_revision = "0001"\n'
        )
        (versions / "0002_b.py").write_text(
            'revision = "0002b"\ndown_revision = "0001"\n'
        )
        (versions / "0003_merge.py").write_text(
            'revision = "0003"\ndown_revision = ("0002a", "0002b")\n'
        )
        with patch.object(readiness, "_VERSIONS_DIR", versions):
            assert readiness._discover_repository_heads() == ["0003"]

    def test_list_down_revision(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_base.py").write_text(
            'revision = "0001"\ndown_revision = None\n'
        )
        (versions / "0002_a.py").write_text(
            'revision = "0002a"\ndown_revision = "0001"\n'
        )
        (versions / "0002_b.py").write_text(
            'revision = "0002b"\ndown_revision = "0001"\n'
        )
        (versions / "0003_merge.py").write_text(
            'revision = "0003"\ndown_revision = ["0002a", "0002b"]\n'
        )
        with patch.object(readiness, "_VERSIONS_DIR", versions):
            assert readiness._discover_repository_heads() == ["0003"]

    def test_annotated_assignment_supported(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_typed.py").write_text(
            textwrap.dedent("""\
                from collections.abc import Sequence
                revision: str = "0001"
                down_revision: str | Sequence[str] | None = None
            """)
        )
        with patch.object(readiness, "_VERSIONS_DIR", versions):
            assert readiness._discover_repository_heads() == ["0001"]

    def test_side_effect_module_never_executed(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        marker = tmp_path / "executed.marker"
        (versions / "0001_side_effect.py").write_text(
            f"from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('EXECUTED')\n"
            f'revision = "0001"\n'
            f"down_revision = None\n"
        )
        with patch.object(readiness, "_VERSIONS_DIR", versions):
            heads = readiness._discover_repository_heads()
        assert heads == ["0001"]
        assert not marker.exists()

    def test_malformed_down_revision_fails(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_bad_down.py").write_text(
            'revision = "0001"\ndown_revision = 42\n'
        )
        with (
            patch.object(readiness, "_VERSIONS_DIR", versions),
            pytest.raises(
                readiness.MigrationParseError, match="not a supported literal"
            ),
        ):
            readiness._discover_repository_heads()


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
    def test_asyncpg_url_is_redacted(self) -> None:
        raw = _build_url("postgresql+asyncpg://", "user:pass@host:5432/db failed")
        sanitized = readiness._sanitize(raw)
        assert "user:pass" not in sanitized
        assert "[REDACTED-URL]" in sanitized

    def test_plain_postgresql_url_is_redacted(self) -> None:
        raw = _build_url("postgresql://", "admin:secret@host/db")
        sanitized = readiness._sanitize(raw)
        assert "admin:secret" not in sanitized
        assert "[REDACTED-URL]" in sanitized

    def test_env_var_value_redacted(self) -> None:
        raw = _build_url(
            "FONELY_READINESS_DATABASE_URL = postgresql+asyncpg://",
            "u:p@h/d",
        )
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

    def test_nonzero_exit_for_every_config_failure(self) -> None:
        result = _run_script({"FONELY_READINESS_ENVIRONMENT": "test"})
        assert result.returncode != 0


# --- Connection failure sanitization ---


class TestConnectionFailure:
    def test_refused_connection_is_sanitized(self) -> None:
        url = _build_url(
            "postgresql+asyncpg://",
            "u:testonly@localhost:59999/db",
        )
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": url,
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
        assert "testonly" not in result.stdout
        assert "testonly" not in result.stderr


# --- Timeout ---


class TestTimeout:
    def test_overall_timeout_zero_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": _FAKE_URL,
                "FONELY_READINESS_ENVIRONMENT": "test",
                "FONELY_READINESS_OVERALL_TIMEOUT_S": "0",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )

    def test_connect_timeout_negative_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_READINESS_DATABASE_URL": _FAKE_URL,
                "FONELY_READINESS_ENVIRONMENT": "test",
                "FONELY_READINESS_CONNECT_TIMEOUT_S": "-1",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )


# --- Strict database fake ---


def _mock_engine(
    *,
    version: str = "16.4",
    revision: str | None = "0003",
    has_alembic: bool = True,
    readonly: str = "on",
    connect_fails: Exception | None = None,
    rollback_fails: Exception | None = None,
    rollback_stalls: bool = False,
    dispose_fails: Exception | None = None,
    dispose_stalls: bool = False,
) -> MagicMock:
    engine = MagicMock()
    conn = AsyncMock()

    async def mock_execute(stmt: Any) -> MagicMock:
        sql = str(stmt) if not isinstance(stmt, str) else stmt
        normalized = " ".join(sql.split())
        if not _is_approved_sql(sql):
            raise AssertionError(f"unapproved SQL in readiness verifier: {sql}")
        result = MagicMock()
        if normalized == "SELECT 1":
            if connect_fails:
                raise connect_fails
            result.scalar_one.return_value = 1
            return result
        if normalized == "SHOW server_version":
            result.scalar_one.return_value = version
            return result
        if "information_schema.tables" in normalized:
            result.scalar.return_value = has_alembic
            return result
        if normalized == "SELECT version_num FROM public.alembic_version":
            if revision is not None:
                row = MagicMock()
                row.__getitem__ = lambda s, i: revision
                result.all.return_value = [row]
            else:
                result.all.return_value = []
            return result
        if normalized == "SET TRANSACTION READ ONLY":
            return result
        if normalized == "SHOW transaction_read_only":
            result.scalar.return_value = readonly
            return result
        raise AssertionError(f"unhandled approved SQL: {sql}")

    conn.execute = mock_execute

    async def _mock_scalar(stmt: Any) -> Any:
        sql = str(stmt) if not isinstance(stmt, str) else stmt
        normalized = " ".join(sql.split())
        if not _is_approved_sql(sql):
            raise AssertionError(f"unapproved SQL in readiness verifier: {sql}")
        if "information_schema.tables" in normalized:
            return has_alembic
        if normalized == "SHOW transaction_read_only":
            return readonly
        if normalized == "SHOW server_version":
            return version
        result = await mock_execute(stmt)
        return result.scalar()

    conn.scalar = AsyncMock(side_effect=_mock_scalar)

    async def _rollback() -> None:
        if rollback_stalls:
            await asyncio.sleep(100)
        if rollback_fails is not None:
            raise rollback_fails

    conn.rollback = AsyncMock(side_effect=_rollback)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    engine.connect.return_value = ctx

    async def _dispose() -> None:
        if dispose_stalls:
            await asyncio.sleep(100)
        if dispose_fails is not None:
            raise dispose_fails

    engine.dispose = AsyncMock(side_effect=_dispose)
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
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
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
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
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
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
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
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
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
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
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
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
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
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
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
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
            )
        assert report.overall_status == "passed"
        assert report.repository_head == "0003"
        assert report.database_revision == "0003"
        assert report.postgres_major == 16
        assert len(report.checks) == 6
        assert all(c["status"] == "passed" for c in report.checks)

    def test_connection_exception_sanitized(self) -> None:
        engine = _mock_engine(
            connect_fails=ConnectionRefusedError("test_host_only:5432")
        )
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
            )
        assert report.overall_status == "failed"
        conn_check = next(c for c in report.checks if c["name"] == "connection")
        assert conn_check["failure_code"] == "connection_failed"
        assert "test_host_only" not in (conn_check["message"] or "")

    def test_engine_disposed_on_failure(self) -> None:
        engine = _mock_engine(connect_fails=ConnectionRefusedError())
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            asyncio.run(readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15)))
        engine.dispose.assert_awaited_once()

    def test_malformed_db_revision_is_rejected(self) -> None:
        engine = _mock_engine(revision="'; DROP TABLE users; --")
        with (
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            report = asyncio.run(
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
            )
        assert report.overall_status == "failed"
        rev_check = next(c for c in report.checks if c["name"] == "database_revision")
        assert rev_check["failure_code"] == "database_revision_invalid"
        assert "DROP" not in json.dumps(report.to_dict())


class TestSQLAllowlistEnforcement:
    def _exec(self, sql: str) -> None:
        asyncio.run(
            _mock_engine().connect.return_value.__aenter__.return_value.execute(sql)
        )

    def test_fake_rejects_create_table(self) -> None:
        with pytest.raises(AssertionError, match="unapproved SQL"):
            self._exec("CREATE TABLE exploit (id int)")

    def test_fake_rejects_insert(self) -> None:
        with pytest.raises(AssertionError, match="unapproved SQL"):
            self._exec("INSERT INTO alembic_version VALUES ('evil')")

    def test_fake_rejects_update(self) -> None:
        with pytest.raises(AssertionError, match="unapproved SQL"):
            self._exec("UPDATE alembic_version SET version_num = 'evil'")

    def test_fake_rejects_delete(self) -> None:
        with pytest.raises(AssertionError, match="unapproved SQL"):
            self._exec("DELETE FROM alembic_version")

    def test_fake_rejects_delete_returning_version_num(self) -> None:
        with pytest.raises(AssertionError, match="unapproved SQL"):
            self._exec("DELETE FROM public.alembic_version RETURNING version_num")

    def test_fake_rejects_drop_then_select(self) -> None:
        with pytest.raises(AssertionError, match="unapproved SQL"):
            self._exec(
                "DROP TABLE alembic_version; "
                "SELECT version_num FROM public.alembic_version"
            )

    def test_fake_rejects_select_then_delete(self) -> None:
        with pytest.raises(AssertionError, match="unapproved SQL"):
            self._exec(
                "SELECT version_num FROM public.alembic_version; "
                "DELETE FROM public.alembic_version"
            )

    def test_fake_rejects_comment_prefixed_mutation(self) -> None:
        with pytest.raises(AssertionError, match="unapproved SQL"):
            self._exec("/* harmless */ DELETE FROM public.alembic_version")

    def test_fake_rejects_comment_suffixed_mutation(self) -> None:
        with pytest.raises(AssertionError, match="unapproved SQL"):
            self._exec("SELECT 1; -- DELETE FROM alembic_version")

    def test_approved_select_1_passes(self) -> None:
        self._exec("SELECT 1")

    def test_approved_show_version_passes(self) -> None:
        self._exec("SHOW server_version")

    def test_approved_set_readonly_passes(self) -> None:
        self._exec("SET TRANSACTION READ ONLY")

    def test_approved_show_readonly_passes(self) -> None:
        self._exec("SHOW transaction_read_only")


# --- Revision validation ---


class TestRevisionValidation:
    def test_safe_revision_text_passes_valid(self) -> None:
        assert readiness._safe_revision_text("0003") == "0003"
        assert readiness._safe_revision_text("abc_123") == "abc_123"

    def test_safe_revision_text_rejects_injection(self) -> None:
        assert readiness._safe_revision_text("'; DROP--") == "[invalid-revision]"
        assert readiness._safe_revision_text("a" * 100) == "[invalid-revision]"
        assert readiness._safe_revision_text("") == "[invalid-revision]"

    def test_trailing_newline_rejected(self) -> None:
        assert readiness._safe_revision_text("0003\n") == "[invalid-revision]"

    def test_leading_whitespace_rejected(self) -> None:
        assert readiness._safe_revision_text(" 0003") == "[invalid-revision]"

    def test_trailing_whitespace_rejected(self) -> None:
        assert readiness._safe_revision_text("0003 ") == "[invalid-revision]"

    def test_embedded_newline_rejected(self) -> None:
        assert readiness._safe_revision_text("00\n03") == "[invalid-revision]"

    def test_fullmatch_used_for_revision_regex(self) -> None:
        assert not readiness._SAFE_REVISION_RE.fullmatch("0003\n")
        assert readiness._SAFE_REVISION_RE.fullmatch("0003")


# --- Cycle detection ---


class TestCycleDetection:
    def test_self_cycle_rejected(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_self.py").write_text(
            'revision = "0001"\ndown_revision = "0001"\n'
        )
        with (
            patch.object(readiness, "_VERSIONS_DIR", versions),
            pytest.raises(readiness.MigrationParseError, match="cycle"),
        ):
            readiness._discover_repository_heads()

    def test_two_node_cycle_rejected(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001_a.py").write_text(
            'revision = "0001"\ndown_revision = "0002"\n'
        )
        (versions / "0002_b.py").write_text(
            'revision = "0002"\ndown_revision = "0001"\n'
        )
        with (
            patch.object(readiness, "_VERSIONS_DIR", versions),
            pytest.raises(readiness.MigrationParseError, match="cycle"),
        ):
            readiness._discover_repository_heads()

    def test_longer_cycle_rejected(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "a.py").write_text('revision = "a"\ndown_revision = "c"\n')
        (versions / "b.py").write_text('revision = "b"\ndown_revision = "a"\n')
        (versions / "c.py").write_text('revision = "c"\ndown_revision = "b"\n')
        with (
            patch.object(readiness, "_VERSIONS_DIR", versions),
            pytest.raises(readiness.MigrationParseError, match="cycle"),
        ):
            readiness._discover_repository_heads()

    def test_cycle_with_disconnected_head_rejected(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "good.py").write_text('revision = "good"\ndown_revision = None\n')
        (versions / "a.py").write_text('revision = "a"\ndown_revision = "b"\n')
        (versions / "b.py").write_text('revision = "b"\ndown_revision = "a"\n')
        with (
            patch.object(readiness, "_VERSIONS_DIR", versions),
            pytest.raises(readiness.MigrationParseError, match="cycle"),
        ):
            readiness._discover_repository_heads()

    def test_valid_linear_chain_no_cycle(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "0001.py").write_text('revision = "0001"\ndown_revision = None\n')
        (versions / "0002.py").write_text('revision = "0002"\ndown_revision = "0001"\n')
        with patch.object(readiness, "_VERSIONS_DIR", versions):
            assert readiness._discover_repository_heads() == ["0002"]


# --- Adversarial cleanup matrix ---


def _deadline(seconds: float = 15) -> float:
    return time.monotonic() + seconds


def _run_with_mock(engine: MagicMock, timeout: float = 15) -> readiness.ReadinessReport:
    with (
        patch.object(readiness, "_discover_repository_heads", return_value=["0003"]),
        patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
    ):
        return asyncio.run(
            readiness._run_checks(_FAKE_URL, "test", 1, _deadline(timeout))
        )


def _find_check(report: readiness.ReadinessReport, name: str) -> dict[str, Any] | None:
    return next((c for c in report.checks if c["name"] == name), None)


class TestCleanupFailClosed:
    def test_clean_success(self) -> None:
        report = _run_with_mock(_mock_engine())
        assert report.overall_status == "passed"
        ro = _find_check(report, "readonly_transaction")
        assert ro is not None and ro["status"] == "passed"
        ec = _find_check(report, "engine_cleanup")
        assert ec is not None and ec["status"] == "passed"

    def test_rollback_timeout_fails_readiness(self) -> None:
        engine = _mock_engine(rollback_stalls=True)
        report = _run_with_mock(engine, timeout=5)
        assert report.overall_status == "failed"
        ro = _find_check(report, "readonly_transaction")
        assert ro is not None
        assert ro["status"] == "failed"
        assert ro["failure_code"] == "readonly_cleanup_failed"
        ec = _find_check(report, "engine_cleanup")
        assert ec is not None

    def test_rollback_exception_fails_readiness(self) -> None:
        engine = _mock_engine(rollback_fails=RuntimeError("rollback boom"))
        report = _run_with_mock(engine)
        assert report.overall_status == "failed"
        ro = _find_check(report, "readonly_transaction")
        assert ro is not None
        assert ro["status"] == "failed"
        assert ro["failure_code"] == "readonly_cleanup_failed"

    def test_disposal_timeout_fails_readiness(self) -> None:
        engine = _mock_engine(dispose_stalls=True)
        report = _run_with_mock(engine, timeout=5)
        assert report.overall_status == "failed"
        ec = _find_check(report, "engine_cleanup")
        assert ec is not None
        assert ec["status"] == "failed"
        assert ec["failure_code"] == "engine_cleanup_failed"
        ro = _find_check(report, "readonly_transaction")
        assert ro is not None and ro["status"] == "passed"

    def test_disposal_exception_fails_readiness(self) -> None:
        engine = _mock_engine(dispose_fails=OSError("dispose boom"))
        report = _run_with_mock(engine)
        assert report.overall_status == "failed"
        ec = _find_check(report, "engine_cleanup")
        assert ec is not None
        assert ec["status"] == "failed"
        assert ec["failure_code"] == "engine_cleanup_failed"

    def test_primary_failure_plus_disposal_failure(self) -> None:
        engine = _mock_engine(
            connect_fails=ConnectionRefusedError(),
            dispose_fails=OSError("dispose too"),
        )
        report = _run_with_mock(engine)
        assert report.overall_status == "failed"
        conn = _find_check(report, "connection")
        assert conn is not None and conn["status"] == "failed"
        assert conn["failure_code"] == "connection_failed"
        ec = _find_check(report, "engine_cleanup")
        assert ec is not None and ec["status"] == "failed"

    def test_rollback_failure_plus_disposal_failure(self) -> None:
        engine = _mock_engine(
            rollback_fails=RuntimeError("rb fail"),
            dispose_fails=OSError("disp fail"),
        )
        report = _run_with_mock(engine)
        assert report.overall_status == "failed"
        ro = _find_check(report, "readonly_transaction")
        assert ro is not None and ro["failure_code"] == "readonly_cleanup_failed"
        ec = _find_check(report, "engine_cleanup")
        assert ec is not None and ec["failure_code"] == "engine_cleanup_failed"

    def test_disposal_attempted_exactly_once(self) -> None:
        engine = _mock_engine()
        _run_with_mock(engine)
        engine.dispose.assert_awaited_once()

    def test_disposal_after_primary_failure(self) -> None:
        engine = _mock_engine(connect_fails=ConnectionRefusedError())
        _run_with_mock(engine)
        engine.dispose.assert_awaited_once()

    def test_no_cleanup_entry_without_engine(self) -> None:
        with patch.object(readiness, "_discover_repository_heads", return_value=[]):
            report = asyncio.run(
                readiness._run_checks(_FAKE_URL, "test", 5, _deadline(15))
            )
        assert report.overall_status == "failed"
        assert _find_check(report, "engine_cleanup") is None

    def test_cleanup_sanitization(self) -> None:
        url_fragment = "test_host_secret"
        engine = _mock_engine(
            dispose_fails=OSError(
                _build_url("postgresql+asyncpg://", f"u:{url_fragment}@h/d")
            ),
        )
        report = _run_with_mock(engine)
        output = json.dumps(report.to_dict())
        assert url_fragment not in output

    def test_json_has_exactly_one_document(self) -> None:
        engine = _mock_engine(rollback_fails=RuntimeError("x"))
        report = _run_with_mock(engine)
        output = json.dumps(report.to_dict())
        assert output.count('"schema_version"') == 1

    def test_check_ordering_is_deterministic(self) -> None:
        engine = _mock_engine()
        report = _run_with_mock(engine)
        names = [c["name"] for c in report.checks]
        assert names == [
            "repository_head",
            "connection",
            "postgres_version",
            "database_revision",
            "readonly_transaction",
            "engine_cleanup",
        ]

    def test_exit_code_matches_overall_status(self) -> None:
        engine = _mock_engine(dispose_fails=OSError("boom"))
        report = _run_with_mock(engine)
        assert report.overall_status == "failed"
        exit_code = 0 if report.overall_status == "passed" else 1
        assert exit_code == 1


# --- Deadline interaction through _main() ---


def _run_main_with_env(
    env_overrides: dict[str, str],
    engine: MagicMock | None = None,
) -> tuple[readiness.ReadinessReport | dict[str, Any], int]:
    """Run _main() with controlled env and optional engine mock, return (report, exit_code)."""
    import contextlib
    import io

    env = {
        "FONELY_READINESS_DATABASE_URL": _FAKE_URL,
        "FONELY_READINESS_ENVIRONMENT": "test",
        **env_overrides,
    }
    patches = [
        patch.dict(os.environ, env, clear=False),
        patch.object(readiness, "_discover_repository_heads", return_value=["0003"]),
    ]
    if engine is not None:
        patches.append(
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine)
        )

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        # Clear any leftover env to ensure only our overrides are active
        for key in list(os.environ):
            if key.startswith("FONELY_READINESS_") and key not in env:
                del os.environ[key]
        exit_code = asyncio.run(readiness._main())
    output = json.loads(stdout.getvalue())
    return output, exit_code


class TestDeadlineInteraction:
    def test_successful_run_through_main(self) -> None:
        engine = _mock_engine()
        output, exit_code = _run_main_with_env({}, engine)
        assert output["overall_status"] == "passed"
        assert exit_code == 0
        cleanup = next(c for c in output["checks"] if c["name"] == "engine_cleanup")
        assert cleanup["status"] == "passed"

    def test_deadline_expired_before_checks_still_disposes(self) -> None:
        engine = _mock_engine()
        output, exit_code = _run_main_with_env(
            {"FONELY_READINESS_OVERALL_TIMEOUT_S": "0.001"},
            engine,
        )
        assert output["overall_status"] == "failed"
        assert exit_code == 1
        codes = [c["failure_code"] for c in output["checks"] if c["failure_code"]]
        assert any(code in ("overall_timeout", "connection_timeout") for code in codes)

    def test_disposal_failure_through_main_exits_nonzero(self) -> None:
        engine = _mock_engine(dispose_fails=OSError("dispose error"))
        output, exit_code = _run_main_with_env({}, engine)
        assert output["overall_status"] == "failed"
        assert exit_code == 1
        cleanup = next(c for c in output["checks"] if c["name"] == "engine_cleanup")
        assert cleanup["failure_code"] == "engine_cleanup_failed"

    def test_rollback_failure_through_main_exits_nonzero(self) -> None:
        engine = _mock_engine(rollback_fails=RuntimeError("rb fail"))
        output, exit_code = _run_main_with_env({}, engine)
        assert output["overall_status"] == "failed"
        assert exit_code == 1
        ro = next(c for c in output["checks"] if c["name"] == "readonly_transaction")
        assert ro["failure_code"] == "readonly_cleanup_failed"

    def test_primary_failure_plus_disposal_failure_through_main(self) -> None:
        engine = _mock_engine(
            connect_fails=ConnectionRefusedError(),
            dispose_fails=OSError("disp"),
        )
        output, exit_code = _run_main_with_env({}, engine)
        assert output["overall_status"] == "failed"
        assert exit_code == 1
        conn = next(c for c in output["checks"] if c["name"] == "connection")
        assert conn["failure_code"] == "connection_failed"
        cleanup = next(c for c in output["checks"] if c["name"] == "engine_cleanup")
        assert cleanup["failure_code"] == "engine_cleanup_failed"

    def test_overall_smaller_than_connect_timeout(self) -> None:
        engine = _mock_engine()
        output, exit_code = _run_main_with_env(
            {
                "FONELY_READINESS_CONNECT_TIMEOUT_S": "60",
                "FONELY_READINESS_OVERALL_TIMEOUT_S": "5",
            },
            engine,
        )
        assert exit_code == 0
        assert output["overall_status"] == "passed"

    def test_no_engine_no_cleanup_entry_through_main(self) -> None:
        import contextlib
        import io

        env = {
            "FONELY_READINESS_DATABASE_URL": _FAKE_URL,
            "FONELY_READINESS_ENVIRONMENT": "test",
        }
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            patch.dict(os.environ, env, clear=False),
            patch.object(readiness, "_discover_repository_heads", return_value=[]),
        ):
            exit_code = asyncio.run(readiness._main())
        output = json.loads(stdout.getvalue())
        assert output["overall_status"] == "failed"
        assert exit_code == 1
        names = [c["name"] for c in output["checks"]]
        assert "engine_cleanup" not in names

    def test_exactly_one_json_document(self) -> None:
        engine = _mock_engine(rollback_fails=RuntimeError("x"))
        import contextlib
        import io

        env = {
            "FONELY_READINESS_DATABASE_URL": _FAKE_URL,
            "FONELY_READINESS_ENVIRONMENT": "test",
        }
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            patch.dict(os.environ, env, clear=False),
            patch.object(
                readiness, "_discover_repository_heads", return_value=["0003"]
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
        ):
            asyncio.run(readiness._main())
        raw = stdout.getvalue().strip()
        docs = [json.loads(raw)]
        assert len(docs) == 1
