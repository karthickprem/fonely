#!/usr/bin/env python3
"""Fail-closed verifier for partition-bound pytest JUnit evidence."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MAX_BYTES = 100 * 1024 * 1024
MAX_CASES = 100_000
VALID_ENVS = ("ci", "local", "staging")


def fail_input(message: str) -> None:
    raise ValueError(message)


def safe_input(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        fail_input(f"{label} must be a regular non-symlink file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_BYTES:
        fail_input(f"{label} has invalid size")
    try:
        return path.read_bytes()
    except OSError as exc:
        fail_input(f"{label} could not be read: {type(exc).__name__}")


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        fail_input("report path is a symlink")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        if tmp.is_symlink():
            fail_input("temporary report path is a symlink")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def digest_nodes(nodes: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(nodes)).encode()).hexdigest()


def validate_inventory(data: object) -> tuple[set[str], set[str]]:
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        fail_input("inventory schema_version must be 2")
    if data.get("valid") is not True or data.get("errors") != []:
        fail_input("inventory is not valid")
    nodes, counts, digests = data.get("nodes"), data.get("counts"), data.get("digests")
    if not all(isinstance(value, dict) for value in (nodes, counts, digests)):
        fail_input("inventory nodes/counts/digests must be objects")
    npg_list, pg_list = nodes.get("non_pg"), nodes.get("pg")
    if not isinstance(npg_list, list) or not isinstance(pg_list, list):
        fail_input("inventory partitions must be arrays")
    if not npg_list or not pg_list:
        fail_input("inventory partitions must both be nonempty")
    if not all(isinstance(n, str) and n for n in [*npg_list, *pg_list]):
        fail_input("inventory contains invalid node IDs")
    if len(npg_list) != len(set(npg_list)) or len(pg_list) != len(set(pg_list)):
        fail_input("inventory contains duplicate node IDs")
    npg, pg = set(npg_list), set(pg_list)
    if npg & pg:
        fail_input("inventory partitions overlap")
    union = npg | pg
    expected_counts = {"non_pg": len(npg), "pg": len(pg), "all": len(union)}
    if any(counts.get(key) != value for key, value in expected_counts.items()):
        fail_input("inventory counts do not match nodes")
    expected_digests = {
        "non_pg": digest_nodes(npg),
        "pg": digest_nodes(pg),
        "all": digest_nodes(union),
    }
    if any(digests.get(key) != value for key, value in expected_digests.items()):
        fail_input("inventory digests do not match nodes")
    return npg, pg


def testcase_outcome(case: ET.Element) -> str:
    if case.find("error") is not None:
        return "error"
    if case.find("failure") is not None:
        return "failed"
    skipped = case.find("skipped")
    if skipped is None:
        return "passed"
    message = (skipped.get("message") or "").lower()
    kind = (skipped.get("type") or "").lower()
    if "xpass" in message:
        return "xpass"
    if "xfail" in kind or "xfail" in message:
        return "xfail"
    return "skipped"


def parse_junit(path: Path, partition: str) -> dict[str, str]:
    raw = safe_input(path, f"{partition} JUnit")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        fail_input(f"{partition} JUnit malformed XML: {exc}")
    if root.tag not in ("testsuite", "testsuites"):
        fail_input(f"{partition} JUnit has invalid root {root.tag!r}")
    cases = list(root.iter("testcase"))
    if not cases or len(cases) > MAX_CASES:
        fail_input(f"{partition} JUnit testcase count is invalid")
    results: dict[str, str] = {}
    for case in cases:
        props = [
            p.get("value")
            for p in case.findall("./properties/property")
            if p.get("name") == "node_id"
        ]
        if len(props) != 1 or not props[0]:
            fail_input(f"{partition} testcase requires exactly one node_id property")
        node = props[0]
        if node in results:
            fail_input(f"duplicate node in {partition} JUnit: {node}")
        results[node] = testcase_outcome(case)
    for suite in root.iter("testsuite"):
        descendants = list(suite.iter("testcase"))
        actual = {
            "tests": len(descendants),
            "failures": sum(testcase_outcome(c) == "failed" for c in descendants),
            "errors": sum(testcase_outcome(c) == "error" for c in descendants),
            "skipped": sum(
                testcase_outcome(c) in ("skipped", "xfail", "xpass")
                for c in descendants
            ),
        }
        for key, value in actual.items():
            declared = suite.get(key)
            if declared is not None and (
                not declared.isdigit() or int(declared) != value
            ):
                fail_input(
                    f"{partition} JUnit {key}={declared!r} contradicts parsed {value}"
                )
    return results


def load_allowlist(path: Path, now: datetime) -> list[dict[str, Any]]:
    try:
        data = json.loads(safe_input(path, "allowlist").decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail_input(f"allowlist malformed: {type(exc).__name__}")
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        fail_input("allowlist schema_version must be 1")
    entries = data.get("entries")
    if not isinstance(entries, list):
        fail_input("allowlist entries must be an array")
    seen: list[tuple[str, frozenset[str]]] = []
    required = {
        "node_id_pattern",
        "owner",
        "issue_url",
        "reason",
        "created_at",
        "expires_at",
        "environments",
    }
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != required:
            fail_input(f"allowlist entry {index} has invalid fields")
        for key in ("node_id_pattern", "owner", "issue_url", "reason"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                fail_input(f"allowlist entry {index} invalid {key}")
        pattern = entry["node_id_pattern"]
        if "*" in pattern or "?" in pattern or "[" in pattern:
            fail_input("multi-node allowlist patterns are not permitted")
        parsed_url = urlparse(entry["issue_url"])
        if (
            parsed_url.scheme not in ("http", "https")
            or not parsed_url.hostname
            or parsed_url.path in ("", "/")
        ):
            fail_input(f"allowlist entry {index} invalid issue_url")
        envs = entry["environments"]
        if (
            not isinstance(envs, list)
            or not envs
            or not all(isinstance(env, str) for env in envs)
            or not set(envs).issubset(VALID_ENVS)
        ):
            fail_input(f"allowlist entry {index} invalid environments")
        try:
            created = datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))
            expires = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            fail_input(
                f"allowlist entry {index} malformed timestamps: {type(exc).__name__}"
            )
        if created.tzinfo is None or expires.tzinfo is None:
            fail_input(f"allowlist entry {index} timestamps must be timezone-aware")
        if created > now:
            fail_input(f"allowlist entry {index} created_at is in the future")
        if expires < created or expires > created + timedelta(days=14):
            fail_input(f"allowlist entry {index} has invalid expiry window")
        identity = (pattern, frozenset(envs))
        if any(
            pattern == old_pattern and identity[1] & old_envs
            for old_pattern, old_envs in seen
        ):
            fail_input(f"allowlist entry {index} overlaps another entry")
        seen.append(identity)
    return entries


def digest_results(non_pg: dict[str, str], pg: dict[str, str]) -> str:
    rows = [f"non_pg:{node}:{outcome}" for node, outcome in sorted(non_pg.items())]
    rows += [f"pg:{node}:{outcome}" for node, outcome in sorted(pg.items())]
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--non-pg-junit", required=True, type=Path)
    parser.add_argument("--pg-junit", required=True, type=Path)
    parser.add_argument("--skip-allowlist", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--environment", choices=VALID_ENVS, default="ci")
    parser.add_argument("--now", help=argparse.SUPPRESS)
    args = parser.parse_args()
    invalid = {"schema_version": 4, "valid": False, "errors": ["not completed"]}
    try:
        atomic_json(args.report, invalid)
        inventory = json.loads(safe_input(args.inventory, "inventory").decode())
        non_pg_expected, pg_expected = validate_inventory(inventory)
        non_pg = parse_junit(args.non_pg_junit, "non_pg")
        pg = parse_junit(args.pg_junit, "pg")
        now = (
            datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            if args.now
            else datetime.now(UTC)
        )
        if now.tzinfo is None:
            fail_input("--now must be timezone-aware")
        allowlist = load_allowlist(args.skip_allowlist, now)
        errors: list[str] = []
        for label, expected, actual in (
            ("non_pg", non_pg_expected, set(non_pg)),
            ("pg", pg_expected, set(pg)),
        ):
            missing, extra = expected - actual, actual - expected
            if missing:
                errors.append(f"{label}: missing {len(missing)}; first={min(missing)}")
            if extra:
                errors.append(f"{label}: extra {len(extra)}; first={min(extra)}")
        if set(non_pg) & pg_expected or set(pg) & non_pg_expected:
            errors.append("JUnit reports contain cross-partition nodes")
        counts = {
            key: 0 for key in ("passed", "failed", "error", "skipped", "xfail", "xpass")
        }
        used: set[int] = set()
        for node, outcome in {**non_pg, **pg}.items():
            counts[outcome] += 1
            if outcome in ("failed", "error", "xpass"):
                errors.append(f"{outcome}: {node}")
            if outcome == "skipped":
                matched = False
                for index, entry in enumerate(allowlist):
                    if (
                        entry["node_id_pattern"] == node
                        and args.environment in entry["environments"]
                    ):
                        expires = datetime.fromisoformat(
                            entry["expires_at"].replace("Z", "+00:00")
                        )
                        if now <= expires:
                            used.add(index)
                            matched = True
                            break
                if not matched:
                    errors.append(f"unexpected skip: {node}")
        body_executed_pg = sum(
            outcome in ("passed", "failed", "error", "xpass") for outcome in pg.values()
        )
        if body_executed_pg == 0:
            errors.append("PG inventory has zero genuinely executed test bodies")
        for index, entry in enumerate(allowlist):
            if args.environment in entry["environments"] and index not in used:
                errors.append(f"unused allowlist entry: {entry['node_id_pattern']}")
        report = {
            "schema_version": 4,
            "valid": not errors,
            "errors": errors,
            "counts": {
                **counts,
                "inventory_non_pg": len(non_pg_expected),
                "inventory_pg": len(pg_expected),
                "executed_non_pg": len(non_pg),
                "executed_pg": len(pg),
                "pg_bodies": body_executed_pg,
            },
            "digest": digest_results(non_pg, pg),
        }
        atomic_json(args.report, report)
        if errors:
            for error in errors:
                print(f"EXECUTION ERROR: {error}", file=sys.stderr)
            raise SystemExit(1)
        print(
            f"Execution valid: {len(non_pg)} non-PG + {len(pg)} PG; "
            f"PG bodies={body_executed_pg}"
        )
    except SystemExit:
        raise
    except (
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        OSError,
    ) as exc:
        invalid["errors"] = [str(exc)]
        with contextlib.suppress(ValueError, OSError):
            atomic_json(args.report, invalid)
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
