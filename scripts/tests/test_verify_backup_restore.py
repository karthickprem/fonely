"""Offline unit tests for PostgreSQL backup-and-restore verification.

Tests configuration guards, safety validation, command construction,
report semantics, and failure classification without requiring a running
PostgreSQL instance.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parent.parent / "verify-backup-restore.py"

_mod_spec = importlib.util.spec_from_file_location("backup_restore", SCRIPT)
assert _mod_spec and _mod_spec.loader
br = importlib.util.module_from_spec(_mod_spec)
sys.modules["backup_restore"] = br
_mod_spec.loader.exec_module(br)


def _build_url(*parts: str) -> str:
    return "".join(parts)


_SOURCE_URL = _build_url(
    "postgresql://", "fonely_test:fonely_test@localhost:5432/fonely_test"
)
_RESTORE_URL = _build_url(
    "postgresql://", "fonely_test:fonely_test@localhost:5432/fonely_test_restore"
)


def _run_script(
    env_overrides: dict[str, str] | None = None,
    *,
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FONELY_BACKUP_")}
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


# --- Configuration guards ---


class TestConfiguration:
    def test_missing_source_fails(self) -> None:
        result = _run_script(
            {
                "FONELY_BACKUP_RESTORE_URL": _RESTORE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        output = _parse_output(result)
        assert output["checks"][0]["failure_code"] == "configuration_missing"

    def test_missing_restore_fails(self) -> None:
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": _SOURCE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_missing"
        )

    def test_missing_environment_fails(self) -> None:
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": _SOURCE_URL,
                "FONELY_BACKUP_RESTORE_URL": _RESTORE_URL,
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_missing"
        )

    def test_same_source_and_target_fails(self) -> None:
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": _SOURCE_URL,
                "FONELY_BACKUP_RESTORE_URL": _SOURCE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"] == "safety_guard_failed"
        )

    def test_database_url_ignored(self) -> None:
        result = _run_script(
            {
                "DATABASE_URL": _SOURCE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_missing"
        )


# --- Safety guards ---


class TestSafetyGuards:
    def test_production_db_name_rejected(self) -> None:
        prod_url = _build_url("postgresql://", "admin:pass@localhost/fonely_prod")
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": prod_url,
                "FONELY_BACKUP_RESTORE_URL": _RESTORE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"] == "safety_guard_failed"
        )

    def test_non_test_user_rejected(self) -> None:
        url = _build_url("postgresql://", "admin:pass@localhost/fonely_test")
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": url,
                "FONELY_BACKUP_RESTORE_URL": _RESTORE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"] == "safety_guard_failed"
        )

    def test_remote_host_rejected(self) -> None:
        url = _build_url(
            "postgresql://", "fonely_test:pass@remote.example.com/fonely_test"
        )
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": url,
                "FONELY_BACKUP_RESTORE_URL": _RESTORE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"] == "safety_guard_failed"
        )

    def test_nonempty_target_same_as_source_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": _SOURCE_URL,
                "FONELY_BACKUP_RESTORE_URL": _SOURCE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"] == "safety_guard_failed"
        )


# --- Sanitization ---


class TestSanitization:
    def test_url_redacted_in_output(self) -> None:
        secret = "SuperSecret" + "Pass99"
        url = _build_url("postgresql://", f"fonely_test:{secret}@localhost/fonely_test")
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": url,
                "FONELY_BACKUP_RESTORE_URL": _RESTORE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert secret not in result.stdout
        assert secret not in result.stderr

    def test_sanitize_covers_postgresql_urls(self) -> None:
        raw = _build_url("postgresql://", "user:pass@host/db failed")
        assert "user:pass" not in br._sanitize(raw)

    def test_sanitize_covers_asyncpg_urls(self) -> None:
        raw = _build_url("postgresql+asyncpg://", "u:p@h/d")
        assert "u:p" not in br._sanitize(raw)


# --- Report contract ---


class TestReportContract:
    def test_report_has_required_fields(self) -> None:
        result = _run_script({"FONELY_BACKUP_ENVIRONMENT": "test"})
        output = _parse_output(result)
        assert output["schema_version"] == 1
        assert "run_id" in output
        assert output["checked_at"]
        assert output["overall_status"] == "failed"
        assert isinstance(output["checks"], list)

    def test_unique_run_ids(self) -> None:
        ids = {br.BackupRestoreReport().run_id for _ in range(50)}
        assert len(ids) == 50

    def test_nonzero_exit_on_failure(self) -> None:
        result = _run_script({"FONELY_BACKUP_ENVIRONMENT": "test"})
        assert result.returncode != 0


# --- Timeout ---


class TestTimeout:
    def test_zero_timeout_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": _SOURCE_URL,
                "FONELY_BACKUP_RESTORE_URL": _RESTORE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
                "FONELY_BACKUP_TIMEOUT_S": "0",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )

    def test_nan_timeout_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": _SOURCE_URL,
                "FONELY_BACKUP_RESTORE_URL": _RESTORE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
                "FONELY_BACKUP_TIMEOUT_S": "nan",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )

    def test_over_limit_timeout_rejected(self) -> None:
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": _SOURCE_URL,
                "FONELY_BACKUP_RESTORE_URL": _RESTORE_URL,
                "FONELY_BACKUP_ENVIRONMENT": "test",
                "FONELY_BACKUP_TIMEOUT_S": "999",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_invalid"
        )


# --- Revision validation ---


class TestRevisionValidation:
    def test_safe_revision_accepts_valid(self) -> None:
        assert br.SAFE_REVISION_RE.fullmatch("0004")

    def test_safe_revision_rejects_injection(self) -> None:
        assert not br.SAFE_REVISION_RE.fullmatch("'; DROP--")
        assert not br.SAFE_REVISION_RE.fullmatch("a" * 100)

    def test_safe_revision_rejects_newline(self) -> None:
        assert not br.SAFE_REVISION_RE.fullmatch("0004\n")
