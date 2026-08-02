#!/usr/bin/env python3
"""Non-destructive database deployment-readiness verifier.

Verifies that a configured PostgreSQL database is reachable, running a
supported version, at the expected Alembic revision, and accepts read-only
transactions.  Emits exactly one JSON document to stdout and exits nonzero
on any failure.

Safe to run against staging or production databases — performs no writes,
migrations, or schema mutations.

Required environment:
    FONELY_READINESS_DATABASE_URL   async PostgreSQL connection URL
    FONELY_READINESS_ENVIRONMENT    deployment label (e.g. staging, github-ci)

Optional:
    FONELY_READINESS_CONNECT_TIMEOUT_S   connection timeout (default 10)
    FONELY_READINESS_OVERALL_TIMEOUT_S   total execution timeout (default 30)
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_PG_MAJORS = frozenset({14, 15, 16, 17})
SCHEMA_VERSION = 1
_BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"
_VERSIONS_DIR = _BACKEND_ROOT / "migrations" / "versions"

_CREDENTIAL_RE = re.compile(
    r"(postgresql\+asyncpg://[^@]*@|password=\S+|"
    r"FONELY_READINESS_DATABASE_URL\s*=\s*\S+)",
    re.IGNORECASE,
)


def _sanitize(message: str) -> str:
    return _CREDENTIAL_RE.sub("[REDACTED]", message)


@dataclass
class CheckResult:
    name: str
    status: str = "skipped"
    duration_s: float = 0.0
    failure_code: str | None = None
    message: str | None = None


@dataclass
class ReadinessReport:
    check_run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    checked_at: str = ""
    environment: str = ""
    overall_status: str = "unknown"
    total_duration_s: float = 0.0
    repository_head: str | None = None
    database_revision: str | None = None
    postgres_major: int | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "check_run_id": self.check_run_id,
            "checked_at": self.checked_at,
            "environment": self.environment,
            "overall_status": self.overall_status,
            "total_duration_s": round(self.total_duration_s, 3),
            "repository_head": self.repository_head,
            "database_revision": self.database_revision,
            "postgres_major": self.postgres_major,
            "checks": self.checks,
        }


def _discover_repository_heads() -> list[str]:
    if not _VERSIONS_DIR.is_dir():
        return []
    revisions: dict[str, str | None] = {}
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        spec = importlib.util.spec_from_file_location(f"_migration_{path.stem}", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except (SyntaxError, ImportError, AttributeError, TypeError, ValueError):
            continue
        rev = getattr(module, "revision", None)
        down = getattr(module, "down_revision", None)
        if isinstance(rev, str):
            revisions[rev] = down if isinstance(down, str) else None
    if not revisions:
        return []
    children = set(revisions.values()) - {None}
    heads = sorted(set(revisions.keys()) - children)
    return heads


def _validate_config() -> tuple[str, str, float, float]:
    url = os.environ.get("FONELY_READINESS_DATABASE_URL", "").strip()
    env_label = os.environ.get("FONELY_READINESS_ENVIRONMENT", "").strip()
    connect_timeout = os.environ.get("FONELY_READINESS_CONNECT_TIMEOUT_S", "10").strip()
    overall_timeout = os.environ.get("FONELY_READINESS_OVERALL_TIMEOUT_S", "30").strip()

    if not url:
        raise ConfigError(
            "configuration_missing", "FONELY_READINESS_DATABASE_URL is not set"
        )
    if not url.startswith("postgresql"):
        raise ConfigError(
            "configuration_invalid", "database URL must use a postgresql driver"
        )
    if not env_label:
        raise ConfigError(
            "configuration_missing", "FONELY_READINESS_ENVIRONMENT is not set"
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", env_label):
        raise ConfigError(
            "configuration_invalid",
            "environment label must be lowercase alphanumeric with hyphens/underscores",
        )

    try:
        ct = float(connect_timeout)
        if ct <= 0 or ct > 120:
            raise ValueError
    except ValueError:
        raise ConfigError(
            "configuration_invalid",
            "connect timeout must be a number between 0 and 120",
        )

    try:
        ot = float(overall_timeout)
        if ot <= 0 or ot > 300:
            raise ValueError
    except ValueError:
        raise ConfigError(
            "configuration_invalid",
            "overall timeout must be a number between 0 and 300",
        )

    return url, env_label, ct, ot


class ConfigError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CheckFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _run_checks(
    url: str,
    env_label: str,
    connect_timeout: float,
    overall_timeout: float,
) -> ReadinessReport:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    report = ReadinessReport(
        checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        environment=env_label,
    )

    heads = _discover_repository_heads()
    head_check = CheckResult(name="repository_head")
    t0 = time.monotonic()
    if not heads:
        head_check.status = "failed"
        head_check.failure_code = "repository_head_missing"
        head_check.message = "no Alembic revisions found"
    elif len(heads) > 1:
        head_check.status = "failed"
        head_check.failure_code = "repository_heads_multiple"
        head_check.message = f"found {len(heads)} heads: {', '.join(heads)}"
    else:
        head_check.status = "passed"
        report.repository_head = heads[0]
    head_check.duration_s = round(time.monotonic() - t0, 3)
    report.checks.append(
        {
            "name": head_check.name,
            "status": head_check.status,
            "duration_s": head_check.duration_s,
            "failure_code": head_check.failure_code,
            "message": head_check.message,
        }
    )

    if head_check.status == "failed":
        report.overall_status = "failed"
        return report

    engine = create_async_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=False,
        connect_args={"timeout": connect_timeout},
    )

    try:
        conn_check = CheckResult(name="connection")
        t0 = time.monotonic()
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            conn_check.status = "passed"
        except asyncio.TimeoutError:
            conn_check.status = "failed"
            conn_check.failure_code = "connection_timeout"
            conn_check.message = "connection timed out"
        except Exception as exc:  # noqa: BLE001
            conn_check.status = "failed"
            conn_check.failure_code = "connection_failed"
            conn_check.message = _sanitize(str(type(exc).__name__))
        conn_check.duration_s = round(time.monotonic() - t0, 3)
        report.checks.append(
            {
                "name": conn_check.name,
                "status": conn_check.status,
                "duration_s": conn_check.duration_s,
                "failure_code": conn_check.failure_code,
                "message": conn_check.message,
            }
        )
        if conn_check.status == "failed":
            report.overall_status = "failed"
            return report

        version_check = CheckResult(name="postgres_version")
        t0 = time.monotonic()
        try:
            async with engine.connect() as conn:
                row = await conn.execute(text("SHOW server_version"))
                version_str = row.scalar_one()
                match = re.match(r"(\d+)", str(version_str))
                if not match:
                    raise CheckFailure(
                        "unsupported_postgres_version",
                        f"cannot parse version: {_sanitize(str(version_str))}",
                    )
                major = int(match.group(1))
                report.postgres_major = major
                if major not in SUPPORTED_PG_MAJORS:
                    raise CheckFailure(
                        "unsupported_postgres_version",
                        f"PostgreSQL {major} is not in supported set {sorted(SUPPORTED_PG_MAJORS)}",
                    )
                version_check.status = "passed"
        except CheckFailure as exc:
            version_check.status = "failed"
            version_check.failure_code = exc.code
            version_check.message = str(exc)
        except Exception as exc:  # noqa: BLE001
            version_check.status = "failed"
            version_check.failure_code = "internal_error"
            version_check.message = _sanitize(type(exc).__name__)
        version_check.duration_s = round(time.monotonic() - t0, 3)
        report.checks.append(
            {
                "name": version_check.name,
                "status": version_check.status,
                "duration_s": version_check.duration_s,
                "failure_code": version_check.failure_code,
                "message": version_check.message,
            }
        )
        if version_check.status == "failed":
            report.overall_status = "failed"
            return report

        revision_check = CheckResult(name="database_revision")
        t0 = time.monotonic()
        try:
            async with engine.connect() as conn:
                has_table = await conn.scalar(
                    text(
                        "SELECT EXISTS ("
                        "  SELECT 1 FROM information_schema.tables "
                        "  WHERE table_schema = 'public' AND table_name = 'alembic_version'"
                        ")"
                    )
                )
                if not has_table:
                    raise CheckFailure(
                        "alembic_version_missing", "alembic_version table not found"
                    )
                rows = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).all()
                if len(rows) == 0:
                    raise CheckFailure(
                        "database_revision_invalid", "alembic_version is empty"
                    )
                if len(rows) > 1:
                    raise CheckFailure(
                        "database_revision_invalid",
                        f"alembic_version has {len(rows)} rows",
                    )
                db_rev = rows[0][0]
                if not isinstance(db_rev, str) or not db_rev.strip():
                    raise CheckFailure(
                        "database_revision_invalid", "revision is empty or null"
                    )
                report.database_revision = db_rev
                if db_rev != report.repository_head:
                    raise CheckFailure(
                        "database_revision_stale",
                        f"database at {db_rev}, repository expects {report.repository_head}",
                    )
                revision_check.status = "passed"
        except CheckFailure as exc:
            revision_check.status = "failed"
            revision_check.failure_code = exc.code
            revision_check.message = str(exc)
        except Exception as exc:  # noqa: BLE001
            revision_check.status = "failed"
            revision_check.failure_code = "internal_error"
            revision_check.message = _sanitize(type(exc).__name__)
        revision_check.duration_s = round(time.monotonic() - t0, 3)
        report.checks.append(
            {
                "name": revision_check.name,
                "status": revision_check.status,
                "duration_s": revision_check.duration_s,
                "failure_code": revision_check.failure_code,
                "message": revision_check.message,
            }
        )
        if revision_check.status == "failed":
            report.overall_status = "failed"
            return report

        readonly_check = CheckResult(name="readonly_transaction")
        t0 = time.monotonic()
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                is_readonly = await conn.scalar(text("SHOW transaction_read_only"))
                if str(is_readonly).lower() != "on":
                    raise CheckFailure(
                        "readonly_check_failed",
                        "transaction did not report read-only mode",
                    )
                readonly_check.status = "passed"
                await conn.rollback()
        except CheckFailure as exc:
            readonly_check.status = "failed"
            readonly_check.failure_code = exc.code
            readonly_check.message = str(exc)
        except Exception as exc:  # noqa: BLE001
            readonly_check.status = "failed"
            readonly_check.failure_code = "readonly_check_failed"
            readonly_check.message = _sanitize(type(exc).__name__)
        readonly_check.duration_s = round(time.monotonic() - t0, 3)
        report.checks.append(
            {
                "name": readonly_check.name,
                "status": readonly_check.status,
                "duration_s": readonly_check.duration_s,
                "failure_code": readonly_check.failure_code,
                "message": readonly_check.message,
            }
        )
        if readonly_check.status == "failed":
            report.overall_status = "failed"
            return report

        report.overall_status = "passed"
        return report

    finally:
        await engine.dispose()


def _emit_failure(
    code: str,
    message: str,
    env_label: str = "",
) -> dict[str, Any]:
    report = ReadinessReport(
        checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        environment=env_label,
        overall_status="failed",
    )
    report.checks.append(
        {
            "name": "configuration",
            "status": "failed",
            "duration_s": 0.0,
            "failure_code": code,
            "message": _sanitize(message),
        }
    )
    return report.to_dict()


async def _main() -> int:
    start = time.monotonic()

    try:
        url, env_label, connect_timeout, overall_timeout = _validate_config()
    except ConfigError as exc:
        result = _emit_failure(exc.code, str(exc))
        print(json.dumps(result, indent=2))
        return 1

    try:
        report = await asyncio.wait_for(
            _run_checks(url, env_label, connect_timeout, overall_timeout),
            timeout=overall_timeout,
        )
    except asyncio.TimeoutError:
        result = _emit_failure("overall_timeout", "overall timeout exceeded", env_label)
        print(json.dumps(result, indent=2))
        return 1
    except Exception as exc:  # noqa: BLE001
        result = _emit_failure(
            "internal_error", _sanitize(type(exc).__name__), env_label
        )
        print(json.dumps(result, indent=2))
        return 1

    report.total_duration_s = round(time.monotonic() - start, 3)
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.overall_status == "passed" else 1


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
