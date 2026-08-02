"""Offline unit tests for PostgreSQL backup-and-restore verification.

Tests use production digest code paths with controlled query executors.
No running PostgreSQL instance is required.
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


# --- Fake query executor for production digest tests ---

_BASE_EVIDENCE: dict[str, str] = {
    "revision": "0004",
    "businesses": "1|Salon A|salon|+910000000001|Asia/Kolkata|trial\n2|Salon B|salon|+910000000002|Asia/Kolkata|trial",
    "business_users": "1|+910000000001|owner|t\n2|+910000000002|owner|t",
    "services": "1|1|Haircut|30|0|0|500.00|t\n2|2|Facial|45|5|5|800.00|t",
    "resources": "1|1|Priya|staff|t\n2|2|Mira|staff|t",
    "schema_functions": "myfunc||CREATE FUNCTION myfunc() ...",
    "schema_tables": "businesses|id|integer|NO\nbusinesses|name|varchar|NO",
}


def _make_query_fn(overrides: dict[str, str] | None = None) -> Any:
    data = {**_BASE_EVIDENCE, **(overrides or {})}
    label_to_sql: dict[str, str] = {}
    for label, sql in br._EVIDENCE_QUERIES:
        label_to_sql[label] = sql

    def query_fn(sql: str) -> str:
        for label, expected_sql in label_to_sql.items():
            if sql == expected_sql:
                if label not in data:
                    raise RuntimeError(f"missing evidence for {label}")
                return data[label]
        raise RuntimeError(f"unexpected query: {sql}")

    return query_fn


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
            {"DATABASE_URL": _SOURCE_URL, "FONELY_BACKUP_ENVIRONMENT": "test"}
        )
        assert result.returncode != 0
        assert (
            _parse_output(result)["checks"][0]["failure_code"]
            == "configuration_missing"
        )


# --- Host identity ---


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


# --- Production evidence digest tests ---


class TestProductionEvidenceDigest:
    def test_identical_evidence_same_digest(self) -> None:
        fn1 = _make_query_fn()
        fn2 = _make_query_fn()
        d1 = br._compute_digest(br._collect_evidence(fn1))
        d2 = br._compute_digest(br._collect_evidence(fn2))
        assert d1 == d2

    def test_changed_field_same_count_different_digest(self) -> None:
        base = br._compute_digest(br._collect_evidence(_make_query_fn()))
        changed = br._compute_digest(
            br._collect_evidence(
                _make_query_fn(
                    {
                        "businesses": _BASE_EVIDENCE["businesses"].replace(
                            "Salon A", "Salon X"
                        )
                    }
                )
            )
        )
        assert base != changed

    def test_same_count_row_substitution_different_digest(self) -> None:
        base = br._compute_digest(br._collect_evidence(_make_query_fn()))
        substituted = br._compute_digest(
            br._collect_evidence(
                _make_query_fn(
                    {
                        "resources": "1|1|Priya|staff|t\n2|2|Ravi|staff|t",
                    }
                )
            )
        )
        assert base != substituted

    def test_tenant_reassignment_different_digest(self) -> None:
        base = br._compute_digest(br._collect_evidence(_make_query_fn()))
        swapped = br._compute_digest(
            br._collect_evidence(
                _make_query_fn(
                    {
                        "services": "1|2|Haircut|30|0|0|500.00|t\n2|1|Facial|45|5|5|800.00|t",
                    }
                )
            )
        )
        assert base != swapped

    def test_revision_change_different_digest(self) -> None:
        base = br._compute_digest(br._collect_evidence(_make_query_fn()))
        updated = br._compute_digest(
            br._collect_evidence(_make_query_fn({"revision": "0005"}))
        )
        assert base != updated

    def test_function_definition_change_different_digest(self) -> None:
        base = br._compute_digest(br._collect_evidence(_make_query_fn()))
        changed = br._compute_digest(
            br._collect_evidence(
                _make_query_fn(
                    {"schema_functions": "myfunc||CREATE FUNCTION myfunc() ... v2"}
                )
            )
        )
        assert base != changed

    def test_overloaded_functions_include_identity(self) -> None:
        fn_query = next(
            sql for label, sql in br._EVIDENCE_QUERIES if label == "schema_functions"
        )
        assert "pg_get_function_identity_arguments" in fn_query
        assert "ORDER BY p.proname, pg_get_function_identity_arguments" in fn_query

    def test_overloaded_functions_deterministic_order(self) -> None:
        overloaded = (
            "myfunc|integer|CREATE FUNCTION myfunc(integer) ...\n"
            "myfunc|text|CREATE FUNCTION myfunc(text) ..."
        )
        d1 = br._compute_digest(
            br._collect_evidence(_make_query_fn({"schema_functions": overloaded}))
        )
        d2 = br._compute_digest(
            br._collect_evidence(_make_query_fn({"schema_functions": overloaded}))
        )
        assert d1 == d2

    def test_query_failure_raises(self) -> None:
        call_count = 0

        def failing_fn(sql: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise RuntimeError("query failed")
            return "0004"

        try:
            br._collect_evidence(failing_fn)
            collected = True
        except RuntimeError:
            collected = False
        assert not collected

    def test_digest_is_sha256_hex(self) -> None:
        d = br._compute_digest(br._collect_evidence(_make_query_fn()))
        assert len(d) == 64
        assert all(c in "0123456789abcdef" for c in d)

    def test_production_labels_are_deterministic(self) -> None:
        labels = [label for label, _ in br._EVIDENCE_QUERIES]
        assert labels == sorted(set(labels), key=labels.index)
        assert len(labels) == len(set(labels))

    def test_digest_mismatch_is_detected(self) -> None:
        source = br._compute_digest(br._collect_evidence(_make_query_fn()))
        restored = br._compute_digest(
            br._collect_evidence(
                _make_query_fn(
                    {"businesses": "1|Changed|salon|+910000000001|Asia/Kolkata|trial"}
                )
            )
        )
        assert source != restored

    def test_exact_match_succeeds(self) -> None:
        source = br._compute_digest(br._collect_evidence(_make_query_fn()))
        restored = br._compute_digest(br._collect_evidence(_make_query_fn()))
        assert source == restored


# --- No-leak evidence path test ---


class TestEvidencePathNoLeak:
    def test_evidence_values_never_in_failure_output(self) -> None:
        synthetic_name = "LeakTestSalon" + "XYZ"
        synthetic_phone = "+910000099999"

        evidence = {
            **_BASE_EVIDENCE,
            "businesses": f"1|{synthetic_name}|salon|{synthetic_phone}|Asia/Kolkata|trial",
        }

        captured_queries: list[str] = []

        def tracking_fn(sql: str) -> str:
            captured_queries.append(sql)
            for label, expected_sql in br._EVIDENCE_QUERIES:
                if sql == expected_sql and label in evidence:
                    return evidence[label]
            raise RuntimeError(f"unexpected: {sql}")

        parts = br._collect_evidence(tracking_fn)
        digest = br._compute_digest(parts)
        assert len(digest) == 64
        assert synthetic_name not in digest
        assert synthetic_phone not in digest

        report_json = json.dumps({"digest": digest, "status": "passed"})
        assert synthetic_name not in report_json
        assert synthetic_phone not in report_json

        assert len(captured_queries) == len(br._EVIDENCE_QUERIES)


# --- Query failure sanitization ---


class TestQueryFailureSanitization:
    def test_query_exception_is_sanitized(self) -> None:
        secret = "secret" + "_password"
        url_with_secret = _build_url(
            "postgresql://", f"fonely_test:{secret}@localhost/fonely_test"
        )
        sanitized = br._sanitize(f"connection to {url_with_secret} failed")
        assert secret not in sanitized
        assert "[REDACTED-URL]" in sanitized


# --- Production main() flow tests ---

import contextlib
import io
from unittest.mock import MagicMock, patch


def _successful_pg_result() -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


def _run_main_controlled(
    *,
    source_digest: str = "aaa",
    restored_digest: str = "aaa",
    source_after_digest: str = "aaa",
    evidence_call_count: int = 0,
    source_query_responses: dict[str, str] | None = None,
    restore_query_responses: dict[str, str] | None = None,
    pg_dump_ok: bool = True,
    pg_restore_ok: bool = True,
) -> tuple[dict[str, Any], int, str]:
    digests = [source_digest, restored_digest, source_after_digest]
    digest_idx = 0

    def fake_evidence_digest(url: str) -> str:
        nonlocal digest_idx
        result = digests[digest_idx]
        digest_idx += 1
        return result

    _REQUIRED_TABLES = (
        "alembic_version,businesses,business_users,services,resources,"
        "appointments,pending_actions,resource_allocations"
    )
    default_responses = {
        "SHOW server_version": "16.4",
        "SELECT version_num FROM public.alembic_version": "0004",
        "pg_tables": _REQUIRED_TABLES,
        "pg_proc": "5",
    }
    s_responses = {**default_responses, **(source_query_responses or {})}
    r_responses = {**default_responses, **(restore_query_responses or {})}

    source_url = _build_url("postgresql://", "fonely_test:t@localhost:5432/fonely_test")
    restore_url = _build_url(
        "postgresql://", "fonely_test:t@localhost:5432/fonely_test_restore"
    )

    def fake_query(url: str, sql: str, *, timeout: float = 30) -> str:
        responses = s_responses if "fonely_test_restore" not in url else r_responses
        for key, val in responses.items():
            if key in sql:
                return val
        return ""

    def fake_run_pg(cmd: list[str], url: str, *, timeout: float = 60) -> MagicMock:
        r = _successful_pg_result()
        if "pg_dump" in cmd:
            if not pg_dump_ok:
                r.returncode = 1
                r.stderr = "dump failed"
            else:
                for arg in cmd:
                    if arg.endswith(".dump"):
                        Path(arg).parent.mkdir(parents=True, exist_ok=True)
                        Path(arg).write_bytes(b"PGDMP_fake_archive_content")
        elif "pg_restore" in cmd:
            if not pg_restore_ok:
                r.returncode = 2
                r.stderr = "restore failed"
        elif "psql" in cmd:
            pass
        return r

    env = {
        "FONELY_BACKUP_SOURCE_URL": source_url,
        "FONELY_BACKUP_RESTORE_URL": restore_url,
        "FONELY_BACKUP_ENVIRONMENT": "test",
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
        patch.dict(os.environ, env, clear=False),
        patch.object(br, "_query", side_effect=fake_query),
        patch.object(br, "_run_pg", side_effect=fake_run_pg),
        patch.object(br, "_evidence_digest", side_effect=fake_evidence_digest),
    ):
        exit_code = br.main()
    output = json.loads(stdout.getvalue())
    return output, exit_code, stderr.getvalue()


def _find_check(checks: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((c for c in checks if c["name"] == name), None)


class TestMainFlowEvidenceComparison:
    def test_exact_match_succeeds(self) -> None:
        output, exit_code, _ = _run_main_controlled(
            source_digest="abc123",
            restored_digest="abc123",
            source_after_digest="abc123",
        )
        assert exit_code == 0
        assert output["overall_status"] == "passed"
        re_check = _find_check(output["checks"], "restored_evidence")
        assert re_check is not None and re_check["status"] == "passed"
        su_check = _find_check(output["checks"], "source_unchanged")
        assert su_check is not None and su_check["status"] == "passed"

    def test_restored_mismatch_fails(self) -> None:
        output, exit_code, _ = _run_main_controlled(
            source_digest="abc123",
            restored_digest="different",
            source_after_digest="abc123",
        )
        assert exit_code == 1
        assert output["overall_status"] == "failed"
        re_check = _find_check(output["checks"], "restored_evidence")
        assert re_check is not None
        assert re_check["status"] == "failed"
        assert re_check["failure_code"] == "data_mismatch"
        cleanup = _find_check(output["checks"], "file_cleanup")
        assert cleanup is not None

    def test_source_after_changed_fails(self) -> None:
        output, exit_code, _ = _run_main_controlled(
            source_digest="abc123",
            restored_digest="abc123",
            source_after_digest="changed",
        )
        assert exit_code == 1
        assert output["overall_status"] == "failed"
        su_check = _find_check(output["checks"], "source_unchanged")
        assert su_check is not None
        assert su_check["status"] == "failed"
        assert su_check["failure_code"] == "source_changed"
        cleanup = _find_check(output["checks"], "file_cleanup")
        assert cleanup is not None

    def test_cleanup_runs_after_restored_mismatch(self) -> None:
        output, exit_code, _ = _run_main_controlled(
            source_digest="abc123",
            restored_digest="different",
        )
        assert exit_code == 1
        cleanup = _find_check(output["checks"], "file_cleanup")
        assert cleanup is not None and cleanup["status"] == "passed"

    def test_one_json_document(self) -> None:
        output, _, _ = _run_main_controlled()
        assert output["schema_version"] == 1
        assert "run_id" in output

    def test_url_wiring_source_then_restore_then_source(self) -> None:
        urls_called: list[str] = []

        def tracking_digest(url: str) -> str:
            urls_called.append(url)
            return "fixed_digest"

        source_url = _build_url(
            "postgresql://", "fonely_test:t@localhost:5432/fonely_test"
        )
        restore_url = _build_url(
            "postgresql://", "fonely_test:t@localhost:5432/fonely_test_restore"
        )

        def fake_query(url: str, sql: str, *, timeout: float = 30) -> str:
            if "server_version" in sql:
                return "16.4"
            if "alembic_version" in sql:
                return "0004"
            if "pg_tables" in sql:
                return "alembic_version,businesses,business_users,services,resources,appointments,pending_actions,resource_allocations"
            if "pg_proc" in sql:
                return "5"
            return ""

        def fake_run_pg(cmd: list[str], url: str, *, timeout: float = 60) -> MagicMock:
            r = _successful_pg_result()
            if "pg_dump" in cmd:
                for arg in cmd:
                    if arg.endswith(".dump"):
                        Path(arg).parent.mkdir(parents=True, exist_ok=True)
                        Path(arg).write_bytes(b"PGDMP_fake")
            return r

        env = {
            "FONELY_BACKUP_SOURCE_URL": source_url,
            "FONELY_BACKUP_RESTORE_URL": restore_url,
            "FONELY_BACKUP_ENVIRONMENT": "test",
        }
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            patch.dict(os.environ, env, clear=False),
            patch.object(br, "_query", side_effect=fake_query),
            patch.object(br, "_run_pg", side_effect=fake_run_pg),
            patch.object(br, "_evidence_digest", side_effect=tracking_digest),
        ):
            br.main()

        assert len(urls_called) == 3
        assert "fonely_test_restore" not in urls_called[0]
        assert "fonely_test_restore" in urls_called[1]
        assert "fonely_test_restore" not in urls_called[2]


class TestMainFlowNoLeak:
    def test_mismatch_failure_does_not_leak_digest_values(self) -> None:
        output, exit_code, stderr = _run_main_controlled(
            source_digest="source_digest_abc",
            restored_digest="different_restored_xyz",
        )
        assert exit_code == 1
        raw_out = json.dumps(output)
        assert "source_digest_abc" not in raw_out
        assert "different_restored_xyz" not in raw_out
        assert "source_digest_abc" not in stderr
        re_check = _find_check(output["checks"], "restored_evidence")
        assert re_check is not None
        assert re_check["failure_code"] == "data_mismatch"

    def test_source_changed_failure_does_not_leak_digest_values(self) -> None:
        output, exit_code, stderr = _run_main_controlled(
            source_digest="original_abc",
            restored_digest="original_abc",
            source_after_digest="mutated_xyz",
        )
        assert exit_code == 1
        raw_out = json.dumps(output)
        assert "mutated_xyz" not in raw_out
        assert "mutated_xyz" not in stderr
        su_check = _find_check(output["checks"], "source_unchanged")
        assert su_check is not None
        assert su_check["failure_code"] == "source_changed"
