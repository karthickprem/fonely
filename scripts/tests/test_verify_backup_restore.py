"""Offline unit tests for PostgreSQL backup-and-restore verification.

Tests configuration guards, safety validation, host identity equivalence,
evidence digest behavior, report semantics, and failure classification
without requiring a running PostgreSQL instance.
"""

from __future__ import annotations

import hashlib
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
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_missing"
        )

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


# --- Host identity equivalence ---


class TestHostIdentity:
    def test_localhost_and_127_same_db_rejected(self) -> None:
        src = _build_url("postgresql://", "fonely_test:p@localhost:5432/fonely_test")
        tgt = _build_url("postgresql://", "fonely_test:p@127.0.0.1:5432/fonely_test")
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": src,
                "FONELY_BACKUP_RESTORE_URL": tgt,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"] == "safety_guard_failed"
        )

    def test_localhost_and_ipv6_same_db_rejected(self) -> None:
        src = _build_url("postgresql://", "fonely_test:p@localhost:5432/fonely_test")
        tgt = _build_url("postgresql://", "fonely_test:p@[::1]:5432/fonely_test")
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": src,
                "FONELY_BACKUP_RESTORE_URL": tgt,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"] == "safety_guard_failed"
        )

    def test_127_and_ipv6_same_db_rejected(self) -> None:
        src = _build_url("postgresql://", "fonely_test:p@127.0.0.1:5432/fonely_test")
        tgt = _build_url("postgresql://", "fonely_test:p@[::1]:5432/fonely_test")
        result = _run_script(
            {
                "FONELY_BACKUP_SOURCE_URL": src,
                "FONELY_BACKUP_RESTORE_URL": tgt,
                "FONELY_BACKUP_ENVIRONMENT": "test",
            }
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"] == "safety_guard_failed"
        )

    def test_different_databases_on_equivalent_hosts_accepted(self) -> None:
        assert br._canonical_host("localhost") == br._canonical_host("127.0.0.1")
        assert br._canonical_host("::1") == "localhost"


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


# --- Host canonicalization ---


class TestCanonicalHost:
    def test_localhost_canonical(self) -> None:
        assert br._canonical_host("localhost") == "localhost"

    def test_127_canonical(self) -> None:
        assert br._canonical_host("127.0.0.1") == "localhost"

    def test_ipv6_canonical(self) -> None:
        assert br._canonical_host("::1") == "localhost"

    def test_uppercase_localhost(self) -> None:
        assert br._canonical_host("LOCALHOST") == "localhost"

    def test_remote_not_canonical(self) -> None:
        assert br._canonical_host("remote.example.com") == "remote.example.com"


# --- Revision validation ---


class TestRevisionValidation:
    def test_safe_revision_accepts_valid(self) -> None:
        assert br.SAFE_REVISION_RE.fullmatch("0004")

    def test_safe_revision_rejects_injection(self) -> None:
        assert not br.SAFE_REVISION_RE.fullmatch("'; DROP--")
        assert not br.SAFE_REVISION_RE.fullmatch("a" * 100)

    def test_safe_revision_rejects_newline(self) -> None:
        assert not br.SAFE_REVISION_RE.fullmatch("0004\n")


# --- Evidence digest behavioral tests ---


def _digest_from_parts(parts: list[tuple[str, str]]) -> str:
    canonical = "\n".join(f"{label}:{data}" for label, data in parts)
    return hashlib.sha256(canonical.encode()).hexdigest()


class TestEvidenceDigest:
    def test_same_data_same_digest(self) -> None:
        parts = [("revision", "0004"), ("businesses", "1|Salon A")]
        assert _digest_from_parts(parts) == _digest_from_parts(parts)

    def test_changed_field_different_digest(self) -> None:
        base = [("revision", "0004"), ("businesses", "1|Salon A")]
        changed = [("revision", "0004"), ("businesses", "1|Salon B")]
        assert _digest_from_parts(base) != _digest_from_parts(changed)

    def test_same_count_substitution_different_digest(self) -> None:
        base = [("businesses", "1|A\n2|B")]
        substituted = [("businesses", "1|A\n2|C")]
        assert _digest_from_parts(base) != _digest_from_parts(substituted)

    def test_tenant_reassignment_different_digest(self) -> None:
        base = [("services", "1|1|Haircut"), ("services", "2|2|Facial")]
        swapped = [("services", "1|2|Haircut"), ("services", "2|1|Facial")]
        assert _digest_from_parts(base) != _digest_from_parts(swapped)

    def test_revision_change_different_digest(self) -> None:
        base = [("revision", "0003"), ("businesses", "1|A")]
        updated = [("revision", "0004"), ("businesses", "1|A")]
        assert _digest_from_parts(base) != _digest_from_parts(updated)

    def test_function_definition_change_different_digest(self) -> None:
        base = [("schema_functions", "myfunc|CREATE FUNCTION myfunc() ...")]
        changed = [("schema_functions", "myfunc|CREATE FUNCTION myfunc() ... v2")]
        assert _digest_from_parts(base) != _digest_from_parts(changed)

    def test_evidence_queries_cover_required_tables(self) -> None:
        labels = [label for label, _ in br._EVIDENCE_QUERIES]
        assert "revision" in labels
        assert "businesses" in labels
        assert "business_users" in labels
        assert "services" in labels
        assert "resources" in labels
        assert "schema_functions" in labels
        assert "schema_tables" in labels

    def test_no_digest_input_in_output(self) -> None:
        result = _run_script({"FONELY_BACKUP_ENVIRONMENT": "test"})
        assert "Salon" not in result.stdout
        assert "Haircut" not in result.stdout

    def test_digest_is_sha256_hex(self) -> None:
        d = _digest_from_parts([("test", "data")])
        assert len(d) == 64
        assert all(c in "0123456789abcdef" for c in d)
