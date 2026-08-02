#!/usr/bin/env python3
"""PostgreSQL logical backup-and-restore verification for disposable test databases.

Proves that an operator can back up a migrated Fonely database, restore it
into a separate empty database, and verify schema/data/revision integrity.

Uses pg_dump (custom format) and pg_restore via subprocess.  Never accesses
production databases — strict disposable-test safety guards are enforced.

Required environment:
    FONELY_BACKUP_SOURCE_URL        postgresql:// source (migrated, seeded)
    FONELY_BACKUP_RESTORE_URL       postgresql:// restore target (empty, disposable)
    FONELY_BACKUP_ENVIRONMENT       deployment label (e.g. github-ci)

Optional:
    FONELY_BACKUP_TIMEOUT_S         overall timeout (default 120, max 600)
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = 1
SAFE_DB_RE = re.compile(r"fonely_test(?:_[a-z0-9_]+)?")
SAFE_REVISION_RE = re.compile(r"[a-zA-Z0-9_]{1,64}")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _sanitize(message: str) -> str:
    sanitized = re.sub(
        r"postgresql(?:\+\w+)?://[^\s'\"<>]+",
        "[REDACTED-URL]",
        message,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"FONELY_BACKUP_\w+_URL\s*=\s*\S+",
        "[REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


def _check_dict(
    name: str,
    status: str,
    duration: float,
    code: str | None = None,
    msg: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "duration_s": round(duration, 3),
        "failure_code": code,
        "message": msg,
    }


def _canonical_host(host: str) -> str:
    lower = host.lower()
    if lower in _LOOPBACK_HOSTS:
        return "localhost"
    return lower


@dataclass
class BackupRestoreReport:
    schema_version: int = SCHEMA_VERSION
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    checked_at: str = ""
    environment: str = ""
    overall_status: str = "unknown"
    total_duration_s: float = 0.0
    postgres_major: int | None = None
    source_revision: str | None = None
    restored_revision: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "checked_at": self.checked_at,
            "environment": self.environment,
            "overall_status": self.overall_status,
            "total_duration_s": round(self.total_duration_s, 3),
            "postgres_major": self.postgres_major,
            "source_revision": self.source_revision,
            "restored_revision": self.restored_revision,
            "checks": self.checks,
        }


class ConfigError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_sync_url(url: str) -> tuple[str, str, str, str, int]:
    parsed = urlparse(url)
    if not parsed.scheme.startswith("postgresql"):
        raise ConfigError("configuration_invalid", "URL must use postgresql scheme")
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    dbname = (parsed.path or "").lstrip("/")
    user = parsed.username or ""
    password = parsed.password or ""
    return host, dbname, user, password, port


def _sync_url(url: str) -> str:
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "")
    return url


def _validate_disposable(label: str, url: str) -> tuple[str, str, str, str, int]:
    host, dbname, user, password, port = _parse_sync_url(_sync_url(url))
    if not dbname:
        raise ConfigError("configuration_invalid", f"{label} missing database name")
    if not SAFE_DB_RE.fullmatch(dbname):
        raise ConfigError(
            "safety_guard_failed",
            f"{label} database must be fonely_test or fonely_test_<suffix>",
        )
    if "test" not in user.lower():
        raise ConfigError(
            "safety_guard_failed",
            f"{label} user must contain 'test'",
        )
    if _canonical_host(host) != "localhost":
        raise ConfigError(
            "safety_guard_failed",
            f"{label} must use a local host",
        )
    return host, dbname, user, password, port


def _validate_config() -> tuple[str, str, str, str, float]:
    source_url = os.environ.get("FONELY_BACKUP_SOURCE_URL", "").strip()
    restore_url = os.environ.get("FONELY_BACKUP_RESTORE_URL", "").strip()
    env_label = os.environ.get("FONELY_BACKUP_ENVIRONMENT", "").strip()
    timeout_str = os.environ.get("FONELY_BACKUP_TIMEOUT_S", "120").strip()

    if not source_url:
        raise ConfigError("configuration_missing", "FONELY_BACKUP_SOURCE_URL not set")
    if not restore_url:
        raise ConfigError("configuration_missing", "FONELY_BACKUP_RESTORE_URL not set")
    if not env_label:
        raise ConfigError("configuration_missing", "FONELY_BACKUP_ENVIRONMENT not set")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", env_label):
        raise ConfigError("configuration_invalid", "invalid environment label")

    _validate_disposable("source", source_url)
    s_host, s_db, _, _, s_port = _parse_sync_url(_sync_url(source_url))
    r_host, r_db, _, _, r_port = _parse_sync_url(_sync_url(restore_url))
    _validate_disposable("target", restore_url)

    if (_canonical_host(s_host), s_port, s_db) == (
        _canonical_host(r_host),
        r_port,
        r_db,
    ):
        raise ConfigError(
            "safety_guard_failed", "source and target must be different databases"
        )

    try:
        timeout = float(timeout_str)
        if timeout <= 0 or timeout > 600 or not math.isfinite(timeout):
            raise ValueError
    except ValueError:
        raise ConfigError("configuration_invalid", "timeout must be 1-600")

    return source_url, restore_url, env_label, timeout_str, timeout


def _pg_env(url: str) -> dict[str, str]:
    host, dbname, user, password, port = _parse_sync_url(_sync_url(url))
    env = os.environ.copy()
    env["PGHOST"] = host
    env["PGPORT"] = str(port)
    env["PGDATABASE"] = dbname
    env["PGUSER"] = user
    env["PGPASSWORD"] = password
    return env


def _run_pg(
    cmd: list[str],
    url: str,
    *,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        env=_pg_env(url),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _query(url: str, sql: str, *, timeout: float = 30) -> str:
    result = _run_pg(
        ["psql", "-t", "-A", "-c", sql],
        url,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(_sanitize(result.stderr.strip()[:200]))
    return result.stdout.strip()


_EVIDENCE_QUERIES = [
    ("revision", "SELECT version_num FROM public.alembic_version"),
    (
        "businesses",
        (
            "SELECT id, name, category, primary_contact_phone, timezone, subscription "
            "FROM businesses ORDER BY id"
        ),
    ),
    (
        "business_users",
        (
            "SELECT business_id, phone, role, is_active "
            "FROM business_users ORDER BY business_id, phone"
        ),
    ),
    (
        "services",
        (
            "SELECT id, business_id, name, duration_minutes, "
            "buffer_before_minutes, buffer_after_minutes, price, is_active "
            "FROM services ORDER BY id"
        ),
    ),
    (
        "resources",
        (
            "SELECT id, business_id, name, resource_type, is_active "
            "FROM resources ORDER BY id"
        ),
    ),
    (
        "schema_functions",
        (
            "SELECT p.proname, pg_get_functiondef(p.oid) "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON p.pronamespace = n.oid "
            "WHERE n.nspname = 'public' "
            "ORDER BY p.proname"
        ),
    ),
    (
        "schema_tables",
        (
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "ORDER BY table_name, ordinal_position"
        ),
    ),
]


def _evidence_digest(url: str) -> str:
    parts: list[str] = []
    for label, sql in _EVIDENCE_QUERIES:
        raw = _query(url, sql)
        parts.append(f"{label}:{raw}")
    canonical = "\n".join(parts)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _cleanup_backup(backup_path: Path | None) -> bool:
    if backup_path is None:
        return True
    ok = True
    try:
        if backup_path.exists():
            backup_path.unlink()
        if backup_path.parent.exists():
            backup_path.parent.rmdir()
    except OSError:
        ok = False
    return ok


def main() -> int:
    start = time.monotonic()
    report = BackupRestoreReport(
        checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    try:
        source_url, restore_url, env_label, _, timeout = _validate_config()
    except ConfigError as exc:
        report.environment = os.environ.get("FONELY_BACKUP_ENVIRONMENT", "")
        report.overall_status = "failed"
        report.checks.append(
            _check_dict(
                "configuration", "failed", time.monotonic() - start, exc.code, str(exc)
            )
        )
        report.total_duration_s = time.monotonic() - start
        print(json.dumps(report.to_dict(), indent=2))
        return 1

    report.environment = env_label
    backup_path: Path | None = None
    failed = False

    try:
        # --- Source version ---
        t0 = time.monotonic()
        try:
            ver = _query(source_url, "SHOW server_version")
            match = re.match(r"(\d+)", ver)
            if not match:
                raise RuntimeError("cannot parse server version")
            major = int(match.group(1))
            report.postgres_major = major
            report.checks.append(
                _check_dict("source_version", "passed", time.monotonic() - t0)
            )
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                _check_dict(
                    "source_version",
                    "failed",
                    time.monotonic() - t0,
                    "source_unreachable",
                    _sanitize(str(exc)[:200]),
                )
            )
            failed = True
            return 1

        # --- Source evidence digest (before) ---
        t0 = time.monotonic()
        try:
            evidence_before = _evidence_digest(source_url)
            rev = _query(
                source_url,
                "SELECT version_num FROM public.alembic_version",
            )
            if not rev or not SAFE_REVISION_RE.fullmatch(rev):
                raise RuntimeError("invalid source revision")
            report.source_revision = rev
            report.checks.append(
                _check_dict("source_evidence", "passed", time.monotonic() - t0)
            )
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                _check_dict(
                    "source_evidence",
                    "failed",
                    time.monotonic() - t0,
                    "source_revision_invalid",
                    _sanitize(str(exc)[:200]),
                )
            )
            failed = True
            return 1

        # --- Backup ---
        t0 = time.monotonic()
        try:
            tmpdir = tempfile.mkdtemp(prefix="fonely-backup-")
            backup_path = Path(tmpdir) / "backup.dump"
            result = _run_pg(
                ["pg_dump", "-Fc", "-f", str(backup_path)],
                source_url,
                timeout=timeout / 3,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    _sanitize(result.stderr.strip()[:200]) or "pg_dump failed"
                )
            if not backup_path.exists() or backup_path.stat().st_size == 0:
                raise RuntimeError("backup file is empty or missing")
            report.checks.append(_check_dict("backup", "passed", time.monotonic() - t0))
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                _check_dict(
                    "backup",
                    "failed",
                    time.monotonic() - t0,
                    "backup_failed",
                    _sanitize(str(exc)[:200]),
                )
            )
            failed = True
            return 1

        # --- Restore (require exit 0) ---
        t0 = time.monotonic()
        try:
            _, r_db, _, _, _ = _parse_sync_url(_sync_url(restore_url))
            result = _run_pg(
                [
                    "pg_restore",
                    "-d",
                    r_db,
                    "--no-owner",
                    "--no-privileges",
                    str(backup_path),
                ],
                restore_url,
                timeout=timeout / 3,
            )
            if result.returncode != 0:
                stderr_safe = _sanitize(result.stderr.strip()[:500])
                raise RuntimeError(stderr_safe or "pg_restore failed")
            report.checks.append(
                _check_dict("restore", "passed", time.monotonic() - t0)
            )
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                _check_dict(
                    "restore",
                    "failed",
                    time.monotonic() - t0,
                    "restore_failed",
                    _sanitize(str(exc)[:200]),
                )
            )
            failed = True
            return 1

        # --- Verify restored revision ---
        t0 = time.monotonic()
        try:
            restored_rev = _query(
                restore_url,
                "SELECT version_num FROM public.alembic_version",
            )
            if not restored_rev or not SAFE_REVISION_RE.fullmatch(restored_rev):
                raise RuntimeError("invalid restored revision")
            report.restored_revision = restored_rev
            if restored_rev != report.source_revision:
                raise RuntimeError(
                    f"revision mismatch: source={report.source_revision}"
                    f" restored={restored_rev}"
                )
            report.checks.append(
                _check_dict("restored_revision", "passed", time.monotonic() - t0)
            )
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                _check_dict(
                    "restored_revision",
                    "failed",
                    time.monotonic() - t0,
                    "revision_mismatch",
                    _sanitize(str(exc)[:200]),
                )
            )
            failed = True
            return 1

        # --- Verify schema objects ---
        t0 = time.monotonic()
        try:
            required_tables = {
                "alembic_version",
                "businesses",
                "business_users",
                "services",
                "resources",
                "appointments",
                "pending_actions",
                "resource_allocations",
            }
            tables_csv = _query(
                restore_url,
                "SELECT string_agg(tablename, ',') FROM pg_tables "
                "WHERE schemaname = 'public'",
            )
            restored_tables = set(tables_csv.split(",")) if tables_csv else set()
            missing = required_tables - restored_tables
            if missing:
                raise RuntimeError(f"missing tables: {sorted(missing)}")
            funcs = _query(
                restore_url,
                "SELECT count(*) FROM pg_proc p "
                "JOIN pg_namespace n ON p.pronamespace = n.oid "
                "WHERE n.nspname = 'public'",
            )
            if int(funcs) == 0:
                raise RuntimeError("no functions restored")
            report.checks.append(
                _check_dict("schema_objects", "passed", time.monotonic() - t0)
            )
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                _check_dict(
                    "schema_objects",
                    "failed",
                    time.monotonic() - t0,
                    "schema_incomplete",
                    _sanitize(str(exc)[:200]),
                )
            )
            failed = True
            return 1

        # --- Verify restored evidence digest ---
        t0 = time.monotonic()
        try:
            restored_digest = _evidence_digest(restore_url)
            if restored_digest != evidence_before:
                raise RuntimeError("restored evidence digest does not match source")
            report.checks.append(
                _check_dict("restored_evidence", "passed", time.monotonic() - t0)
            )
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                _check_dict(
                    "restored_evidence",
                    "failed",
                    time.monotonic() - t0,
                    "data_mismatch",
                    _sanitize(str(exc)[:200]),
                )
            )
            failed = True
            return 1

        # --- Source unchanged ---
        t0 = time.monotonic()
        try:
            evidence_after = _evidence_digest(source_url)
            if evidence_after != evidence_before:
                raise RuntimeError("source evidence changed during backup/restore")
            report.checks.append(
                _check_dict("source_unchanged", "passed", time.monotonic() - t0)
            )
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                _check_dict(
                    "source_unchanged",
                    "failed",
                    time.monotonic() - t0,
                    "source_changed",
                    _sanitize(str(exc)[:200]),
                )
            )
            failed = True

    finally:
        # --- File cleanup ---
        t0 = time.monotonic()
        file_ok = _cleanup_backup(backup_path)
        report.checks.append(
            _check_dict(
                "file_cleanup",
                "passed" if file_ok else "failed",
                time.monotonic() - t0,
                None if file_ok else "cleanup_failed",
                None if file_ok else "temporary files could not be removed",
            )
        )
        if not file_ok:
            failed = True

        report.overall_status = "failed" if failed else "passed"
        report.total_duration_s = time.monotonic() - start
        print(json.dumps(report.to_dict(), indent=2))

    return 0 if report.overall_status == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
