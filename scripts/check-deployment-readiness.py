#!/usr/bin/env python3
"""Non-destructive database deployment-readiness verifier.

Verifies that a configured PostgreSQL database is reachable, running a
supported version, at the expected Alembic revision, and accepts read-only
transactions.  Emits exactly one JSON document to stdout and exits nonzero
on any failure.

Safe to run against staging or production databases — performs no writes,
migrations, or schema mutations.  Migration metadata is discovered via
static AST parsing; migration modules are never imported or executed.

Required environment:
    FONELY_READINESS_DATABASE_URL   postgresql+asyncpg:// connection URL
    FONELY_READINESS_ENVIRONMENT    deployment label (e.g. staging, github-ci)

Optional:
    FONELY_READINESS_CONNECT_TIMEOUT_S   connection timeout (default 10)
    FONELY_READINESS_OVERALL_TIMEOUT_S   orchestration deadline (default 30)
"""

from __future__ import annotations

import ast
import asyncio
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SUPPORTED_PG_MAJORS = frozenset({14, 15, 16, 17})
SCHEMA_VERSION = 1
CLEANUP_ALLOWANCE_S = 5.0
_BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"
_VERSIONS_DIR = _BACKEND_ROOT / "migrations" / "versions"

_SAFE_REVISION_RE = re.compile(r"[a-zA-Z0-9_]{1,64}")


def _sanitize(message: str) -> str:
    sanitized = re.sub(
        r"postgresql(?:\+\w+)?://[^\s'\"<>]+",
        "[REDACTED-URL]",
        message,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"FONELY_READINESS_DATABASE_URL\s*=\s*\S+",
        "FONELY_READINESS_DATABASE_URL=[REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


def _safe_revision_text(value: str) -> str:
    if _SAFE_REVISION_RE.fullmatch(value):
        return value
    return "[invalid-revision]"


def _check_dict(c: CheckResult) -> dict[str, Any]:
    return {
        "name": c.name,
        "status": c.status,
        "duration_s": c.duration_s,
        "failure_code": c.failure_code,
        "message": c.message,
    }


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


def _extract_literal_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _extract_down_revision(node: ast.expr) -> list[str] | None:
    if isinstance(node, ast.Constant):
        if node.value is None:
            return []
        if isinstance(node.value, str):
            return [node.value]
        return None
    if isinstance(node, (ast.Tuple, ast.List)):
        parents: list[str] = []
        for elt in node.elts:
            val = _extract_literal_string(elt)
            if val is None:
                return None
            parents.append(val)
        return parents
    return None


class MigrationParseError(Exception):
    pass


def _discover_repository_heads() -> list[str]:
    if not _VERSIONS_DIR.is_dir():
        raise MigrationParseError("migration versions directory not found")
    revisions: dict[str, list[str]] = {}
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MigrationParseError(
                f"cannot read {path.name}: {type(exc).__name__}"
            ) from exc
        try:
            tree = ast.parse(source, filename=path.name)
        except SyntaxError as exc:
            raise MigrationParseError(
                f"syntax error in {path.name}: {exc.msg}"
            ) from exc

        rev_value: str | None = None
        down_value: list[str] | None = None
        rev_found = False
        down_found = False
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.value is not None:
                    name = node.target.id
                    if name == "revision":
                        rev_value = _extract_literal_string(node.value)
                        rev_found = True
                    elif name == "down_revision":
                        down_value = _extract_down_revision(node.value)
                        down_found = True
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target.id == "revision":
                            rev_value = _extract_literal_string(node.value)
                            rev_found = True
                        elif target.id == "down_revision":
                            down_value = _extract_down_revision(node.value)
                            down_found = True

        if not rev_found:
            raise MigrationParseError(f"{path.name}: missing revision")
        if rev_value is None:
            raise MigrationParseError(f"{path.name}: revision is not a literal string")
        if not _SAFE_REVISION_RE.fullmatch(rev_value):
            raise MigrationParseError(f"{path.name}: malformed revision identifier")
        if not down_found:
            raise MigrationParseError(f"{path.name}: missing down_revision")
        if down_value is None:
            raise MigrationParseError(
                f"{path.name}: down_revision is not a supported literal"
            )
        if rev_value in revisions:
            raise MigrationParseError(f"duplicate revision: {rev_value}")
        revisions[rev_value] = down_value

    if not revisions:
        raise MigrationParseError("no migration revisions found")

    all_parents: set[str] = set()
    for parents in revisions.values():
        for p in parents:
            if p not in revisions:
                raise MigrationParseError(f"missing parent revision: {p}")
            all_parents.add(p)

    visited: set[str] = set()
    in_stack: set[str] = set()

    def _visit(rev: str) -> None:
        if rev in in_stack:
            raise MigrationParseError(f"migration graph contains a cycle at {rev}")
        if rev in visited:
            return
        in_stack.add(rev)
        for parent in revisions.get(rev, []):
            _visit(parent)
        in_stack.remove(rev)
        visited.add(rev)

    for rev in revisions:
        _visit(rev)

    heads = sorted(set(revisions.keys()) - all_parents)
    return heads


def _validate_config() -> tuple[str, str, float, float]:
    url = os.environ.get("FONELY_READINESS_DATABASE_URL", "").strip()
    env_label = os.environ.get("FONELY_READINESS_ENVIRONMENT", "").strip()
    connect_timeout_str = os.environ.get(
        "FONELY_READINESS_CONNECT_TIMEOUT_S", "10"
    ).strip()
    overall_timeout_str = os.environ.get(
        "FONELY_READINESS_OVERALL_TIMEOUT_S", "30"
    ).strip()

    if not url:
        raise ConfigError(
            "configuration_missing", "FONELY_READINESS_DATABASE_URL is not set"
        )

    if not url.startswith("postgresql+asyncpg://"):
        raise ConfigError(
            "configuration_invalid",
            "database URL must use the postgresql+asyncpg:// scheme",
        )
    try:
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.path or parsed.path == "/":
            raise ValueError("missing host or database")
    except Exception:  # noqa: BLE001
        raise ConfigError(
            "configuration_invalid", "database URL is structurally malformed"
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
        ct = float(connect_timeout_str)
        if not math.isfinite(ct) or ct <= 0 or ct > 120:
            raise ValueError
    except ValueError:
        raise ConfigError(
            "configuration_invalid",
            "connect timeout must be a finite number between 0 and 120",
        )

    try:
        ot = float(overall_timeout_str)
        if not math.isfinite(ot) or ot <= 0 or ot > 300:
            raise ValueError
    except ValueError:
        raise ConfigError(
            "configuration_invalid",
            "overall timeout must be a finite number between 0 and 300",
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


async def _bounded_cleanup(
    coro: Any,
    timeout: float,
) -> str | None:
    try:
        await asyncio.wait_for(coro, timeout=max(timeout, 0.1))
    except asyncio.TimeoutError:
        return "timed out"
    except Exception as exc:  # noqa: BLE001
        return _sanitize(type(exc).__name__)
    return None


def _op_timeout(connect_timeout: float, deadline: float) -> float:
    remaining = deadline - time.monotonic()
    return max(min(connect_timeout, remaining), 0.1)


async def _run_checks(
    url: str,
    env_label: str,
    connect_timeout: float,
    deadline: float,
) -> ReadinessReport:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    report = ReadinessReport(
        checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        environment=env_label,
    )

    head_check = CheckResult(name="repository_head")
    t0 = time.monotonic()
    try:
        heads = _discover_repository_heads()
        if len(heads) == 0:
            head_check.status = "failed"
            head_check.failure_code = "repository_head_missing"
            head_check.message = "no Alembic heads found"
        elif len(heads) > 1:
            head_check.status = "failed"
            head_check.failure_code = "repository_heads_multiple"
            safe = ", ".join(_safe_revision_text(h) for h in heads)
            head_check.message = f"found {len(heads)} heads: {safe}"
        else:
            head_check.status = "passed"
            report.repository_head = heads[0]
    except MigrationParseError as exc:
        head_check.status = "failed"
        head_check.failure_code = "repository_head_missing"
        head_check.message = str(exc)
    head_check.duration_s = round(time.monotonic() - t0, 3)
    report.checks.append(_check_dict(head_check))

    if head_check.status == "failed":
        report.overall_status = "failed"
        return report

    if time.monotonic() >= deadline:
        report.checks.append(
            _check_dict(
                CheckResult(
                    name="overall_timeout",
                    status="failed",
                    failure_code="overall_timeout",
                    message="orchestration deadline exceeded before database checks",
                )
            )
        )
        report.overall_status = "failed"
        return report

    engine = create_async_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=False,
        connect_args={"timeout": connect_timeout},
    )

    primary_failed = False

    try:
        if time.monotonic() >= deadline:
            report.checks.append(
                _check_dict(
                    CheckResult(
                        name="overall_timeout",
                        status="failed",
                        failure_code="overall_timeout",
                        message="orchestration deadline exceeded",
                    )
                )
            )
            primary_failed = True
            return report

        conn_check = CheckResult(name="connection")
        t0 = time.monotonic()
        try:
            async with engine.connect() as conn:
                await asyncio.wait_for(
                    conn.execute(text("SELECT 1")),
                    timeout=_op_timeout(connect_timeout, deadline),
                )
            conn_check.status = "passed"
        except asyncio.TimeoutError:
            conn_check.status = "failed"
            conn_check.failure_code = "connection_timeout"
            conn_check.message = "connection timed out"
        except Exception as exc:  # noqa: BLE001
            conn_check.status = "failed"
            conn_check.failure_code = "connection_failed"
            conn_check.message = _sanitize(type(exc).__name__)
        conn_check.duration_s = round(time.monotonic() - t0, 3)
        report.checks.append(_check_dict(conn_check))
        if conn_check.status == "failed":
            primary_failed = True
            return report

        if time.monotonic() >= deadline:
            report.checks.append(
                _check_dict(
                    CheckResult(
                        name="overall_timeout",
                        status="failed",
                        failure_code="overall_timeout",
                        message="orchestration deadline exceeded after connection",
                    )
                )
            )
            primary_failed = True
            return report

        version_check = CheckResult(name="postgres_version")
        t0 = time.monotonic()
        try:
            async with engine.connect() as conn:
                row = await asyncio.wait_for(
                    conn.execute(text("SHOW server_version")),
                    timeout=_op_timeout(connect_timeout, deadline),
                )
                version_str = row.scalar_one()
                match = re.match(r"(\d+)", str(version_str))
                if not match:
                    raise CheckFailure(
                        "unsupported_postgres_version",
                        "cannot parse server version",
                    )
                major = int(match.group(1))
                report.postgres_major = major
                if major not in SUPPORTED_PG_MAJORS:
                    raise CheckFailure(
                        "unsupported_postgres_version",
                        f"PostgreSQL {major} is not in supported set"
                        f" {sorted(SUPPORTED_PG_MAJORS)}",
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
        report.checks.append(_check_dict(version_check))
        if version_check.status == "failed":
            primary_failed = True
            return report

        if time.monotonic() >= deadline:
            report.checks.append(
                _check_dict(
                    CheckResult(
                        name="overall_timeout",
                        status="failed",
                        failure_code="overall_timeout",
                        message="orchestration deadline exceeded after version check",
                    )
                )
            )
            primary_failed = True
            return report

        revision_check = CheckResult(name="database_revision")
        t0 = time.monotonic()
        try:
            async with engine.connect() as conn:
                has_table = await asyncio.wait_for(
                    conn.scalar(
                        text(
                            "SELECT EXISTS ("
                            "  SELECT 1 FROM information_schema.tables "
                            "  WHERE table_schema = 'public'"
                            "    AND table_name = 'alembic_version'"
                            ")"
                        )
                    ),
                    timeout=_op_timeout(connect_timeout, deadline),
                )
                if not has_table:
                    raise CheckFailure(
                        "alembic_version_missing",
                        "public.alembic_version table not found",
                    )
                rows = (
                    await asyncio.wait_for(
                        conn.execute(
                            text("SELECT version_num FROM public.alembic_version")
                        ),
                        timeout=_op_timeout(connect_timeout, deadline),
                    )
                ).all()
                if len(rows) == 0:
                    raise CheckFailure(
                        "database_revision_invalid",
                        "public.alembic_version is empty",
                    )
                if len(rows) > 1:
                    raise CheckFailure(
                        "database_revision_invalid",
                        f"public.alembic_version has {len(rows)} rows",
                    )
                db_rev = rows[0][0]
                if (
                    not isinstance(db_rev, str)
                    or not db_rev.strip()
                    or not _SAFE_REVISION_RE.fullmatch(db_rev)
                ):
                    raise CheckFailure(
                        "database_revision_invalid",
                        "revision value is empty, null, or malformed",
                    )
                report.database_revision = db_rev
                if db_rev != report.repository_head:
                    raise CheckFailure(
                        "database_revision_stale",
                        f"database at {_safe_revision_text(db_rev)},"
                        f" repository expects {report.repository_head}",
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
        report.checks.append(_check_dict(revision_check))
        if revision_check.status == "failed":
            primary_failed = True
            return report

        if time.monotonic() >= deadline:
            report.checks.append(
                _check_dict(
                    CheckResult(
                        name="overall_timeout",
                        status="failed",
                        failure_code="overall_timeout",
                        message="orchestration deadline exceeded after revision check",
                    )
                )
            )
            primary_failed = True
            return report

        readonly_check = CheckResult(name="readonly_transaction")
        t0 = time.monotonic()
        try:
            async with engine.connect() as conn:
                await asyncio.wait_for(
                    conn.execute(text("SET TRANSACTION READ ONLY")),
                    timeout=_op_timeout(connect_timeout, deadline),
                )
                is_readonly = await asyncio.wait_for(
                    conn.scalar(text("SHOW transaction_read_only")),
                    timeout=_op_timeout(connect_timeout, deadline),
                )
                if str(is_readonly).lower() != "on":
                    raise CheckFailure(
                        "readonly_check_failed",
                        "transaction did not report read-only mode",
                    )
                rollback_err = await _bounded_cleanup(
                    conn.rollback(), CLEANUP_ALLOWANCE_S
                )
                if rollback_err is not None:
                    readonly_check.status = "failed"
                    readonly_check.failure_code = "readonly_cleanup_failed"
                    readonly_check.message = f"rollback failed: {rollback_err}"
                else:
                    readonly_check.status = "passed"
        except CheckFailure as exc:
            readonly_check.status = "failed"
            readonly_check.failure_code = exc.code
            readonly_check.message = str(exc)
        except Exception as exc:  # noqa: BLE001
            readonly_check.status = "failed"
            readonly_check.failure_code = "readonly_check_failed"
            readonly_check.message = _sanitize(type(exc).__name__)
        readonly_check.duration_s = round(time.monotonic() - t0, 3)
        report.checks.append(_check_dict(readonly_check))
        if readonly_check.status == "failed":
            primary_failed = True

    finally:
        disposal_check = CheckResult(name="engine_cleanup")
        t0 = time.monotonic()
        disposal_err = await _bounded_cleanup(engine.dispose(), CLEANUP_ALLOWANCE_S)
        if disposal_err is not None:
            disposal_check.status = "failed"
            disposal_check.failure_code = "engine_cleanup_failed"
            disposal_check.message = f"engine disposal failed: {disposal_err}"
        else:
            disposal_check.status = "passed"
        disposal_check.duration_s = round(time.monotonic() - t0, 3)
        report.checks.append(_check_dict(disposal_check))

        if primary_failed or disposal_check.status == "failed":
            report.overall_status = "failed"
        else:
            report.overall_status = "passed"

    return report


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

    deadline = start + overall_timeout

    report = await _run_checks(url, env_label, connect_timeout, deadline)

    report.total_duration_s = round(time.monotonic() - start, 3)
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.overall_status == "passed" else 1


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
