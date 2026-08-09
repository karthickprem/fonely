#!/usr/bin/env python3
"""Verify every inventoried test has exactly one JUnit execution result.

Binds each JUnit input to its declared non-PG or PG partition. Rejects
missing, extra, duplicate, swapped/cross-partition nodes, zero PG,
unexpected skips, strict xpass, and test failures/errors.

Exit 0 on valid execution, 1 on policy violation, 2 on input/safety error.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

_MAX_FILE_BYTES = 100 * 1024 * 1024
_MAX_TESTCASES = 100_000
_MAX_ALLOWLIST_EXPIRY_DAYS = 14
_VALID_ENVIRONMENTS = {"ci", "local", "staging"}


def _safe_path(path: Path, label: str) -> None:
    if not path.is_file():
        raise ValueError(f"{label} not found: {path}")
    if path.is_symlink():
        raise ValueError(f"{label} is a symlink")
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError(f"{label} exceeds size limit")


def _safe_xml(path: Path, label: str) -> ET.Element:
    _safe_path(path, label)
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise ValueError(f"{label} is malformed XML: {exc}") from exc
    root = tree.getroot()
    if root.tag not in ("testsuite", "testsuites"):
        raise ValueError(f"{label} root is {root.tag!r}; expected testsuite/testsuites")
    return root


def _classname_to_node(classname: str, name: str) -> str:
    parts = classname.split(".")
    path_parts: list[str] = []
    class_parts: list[str] = []
    for i, part in enumerate(parts):
        if part and part[0].isupper() and i > 0:
            class_parts = parts[i:]
            break
        path_parts.append(part)
    path = "/".join(path_parts) + ".py" if path_parts else ""
    if path and class_parts:
        return f"{path}::{'::'.join(class_parts)}::{name}"
    if path:
        return f"{path}::{name}"
    return name


def _parse_junit(path: Path, partition: str) -> dict[str, dict[str, str]]:
    root = _safe_xml(path, f"{partition} JUnit")
    results: dict[str, dict[str, str]] = {}
    total = 0
    for suite in root.iter("testsuite"):
        for attr in ("failures", "errors"):
            raw = suite.get(attr, "0")
            if not raw.isdigit():
                raise ValueError(f"{partition} JUnit invalid suite {attr}={raw!r}")
            if int(raw) > 0 and not any(
                tc.find(attr[:-1] if attr.endswith("s") else attr) is not None
                for tc in suite.findall("testcase")
            ):
                raise ValueError(
                    f"{partition} JUnit reports suite-level {attr}={raw} "
                    "without matching testcase outcome"
                )
    for total, tc in enumerate(root.iter("testcase"), start=1):
        if total > _MAX_TESTCASES:
            raise ValueError(f"{partition} exceeds testcase limit")
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        if not name:
            raise ValueError(f"{partition} testcase missing name")

        node_prop = None
        props = tc.find("properties")
        if props is not None:
            for prop in props.iter("property"):
                if prop.get("name") == "node_id":
                    node_prop = prop.get("value")
                    break
        node_id = node_prop or _classname_to_node(classname, name)

        outcome = "passed"
        skip_el = tc.find("skipped")
        fail_el = tc.find("failure")
        error_el = tc.find("error")
        if error_el is not None:
            outcome = "error"
        elif fail_el is not None:
            outcome = "failed"
        elif skip_el is not None:
            msg = (skip_el.get("message") or "").lower()
            skip_type = (skip_el.get("type") or "").lower()
            if "xpass" in msg or "[xpass" in msg:
                outcome = "xpass"
            elif "xfail" in skip_type or "xfail" in msg:
                outcome = "xfail"
            else:
                outcome = "skipped"

        if node_id in results:
            raise ValueError(f"duplicate node in {partition} JUnit: {node_id}")
        results[node_id] = {
            "outcome": outcome,
            "source": str(path),
            "partition": partition,
        }
    return results


def _load_allowlist(path: Path) -> list[dict]:
    _safe_path(path, "allowlist")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"allowlist malformed JSON: {exc}") from exc
    if not isinstance(data, dict) or "entries" not in data:
        raise ValueError("allowlist missing 'entries'")
    entries = data["entries"]
    if not isinstance(entries, list):
        raise ValueError("allowlist entries must be an array")
    required = {
        "node_id_pattern",
        "owner",
        "reason",
        "issue_url",
        "created_at",
        "expires_at",
        "environments",
    }
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"allowlist entry {i} must be an object")
        missing = required - set(entry)
        if missing:
            raise ValueError(f"allowlist entry {i} missing: {sorted(missing)}")
        for key in ("node_id_pattern", "owner", "reason", "issue_url"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise ValueError(f"allowlist entry {i} invalid {key}")
        pattern = entry["node_id_pattern"]
        if pattern in ("*", "tests/*") or "**" in pattern:
            raise ValueError(f"allowlist entry {i} overbroad pattern: {pattern!r}")
        if not entry["issue_url"].startswith(("https://", "http://")):
            raise ValueError(f"allowlist entry {i} invalid issue_url")
        envs = entry["environments"]
        if (
            not isinstance(envs, list)
            or not envs
            or not all(isinstance(e, str) for e in envs)
            or not set(envs).issubset(_VALID_ENVIRONMENTS)
        ):
            raise ValueError(f"allowlist entry {i} invalid environments")
        try:
            created = datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))
            expires = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"allowlist entry {i} malformed dates") from exc
        if created.tzinfo is None or expires.tzinfo is None:
            raise ValueError(f"allowlist entry {i} timestamps must be aware")
        if expires < created:
            raise ValueError(f"allowlist entry {i} expires before creation")
        if expires > created + timedelta(days=_MAX_ALLOWLIST_EXPIRY_DAYS):
            raise ValueError(
                f"allowlist entry {i} expiry exceeds {_MAX_ALLOWLIST_EXPIRY_DAYS} days"
            )
    return entries


def _match_pattern(pattern: str, node_id: str) -> bool:
    if "*" not in pattern:
        return pattern == node_id
    regex = re.escape(pattern).replace(r"\*", "[^/]*")
    return bool(re.fullmatch(regex, node_id))


def _nodes_digest(nodes: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(nodes)).encode()).hexdigest()


def _validate_inventory(inventory: object) -> tuple[set[str], set[str]]:
    if not isinstance(inventory, dict) or inventory.get("schema_version") != 2:
        raise ValueError("inventory schema_version must be 2")
    if inventory.get("valid") is not True or inventory.get("errors") != []:
        raise RuntimeError("partition inventory is invalid")
    nodes = inventory.get("nodes")
    counts = inventory.get("counts")
    digests = inventory.get("digests")
    if not isinstance(nodes, dict) or not isinstance(counts, dict) or not isinstance(digests, dict):
        raise ValueError("inventory missing nodes/counts/digests objects")
    npg_list, pg_list = nodes.get("non_pg"), nodes.get("pg")
    if not isinstance(npg_list, list) or not isinstance(pg_list, list):
        raise ValueError("inventory node partitions must be arrays")
    if not all(isinstance(n, str) and n for n in [*npg_list, *pg_list]):
        raise ValueError("inventory contains invalid node IDs")
    if len(npg_list) != len(set(npg_list)) or len(pg_list) != len(set(pg_list)):
        raise ValueError("inventory contains duplicate node IDs")
    non_pg, pg = set(npg_list), set(pg_list)
    if non_pg & pg:
        raise ValueError("inventory partitions overlap")
    if counts.get("non_pg") != len(non_pg) or counts.get("pg") != len(pg):
        raise ValueError("inventory counts do not match node sets")
    if counts.get("all") != len(non_pg | pg):
        raise ValueError("inventory all count does not match union")
    if digests.get("non_pg") != _nodes_digest(non_pg) or digests.get("pg") != _nodes_digest(pg):
        raise ValueError("inventory partition digest mismatch")
    if digests.get("all") != _nodes_digest(non_pg | pg):
        raise ValueError("inventory all digest mismatch")
    return non_pg, pg


def _outcome_digest(non_pg: dict[str, dict[str, str]], pg: dict[str, dict[str, str]]) -> str:
    rows = [f"non_pg:{nid}:{result['outcome']}" for nid, result in sorted(non_pg.items())] + [
        f"pg:{nid}:{result['outcome']}" for nid, result in sorted(pg.items())
    ]
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _atomic_write(path: Path, data: dict) -> None:
    if path.exists() and path.is_symlink():
        raise ValueError("report path is a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify test execution truth")
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--non-pg-junit", required=True, type=Path)
    parser.add_argument("--pg-junit", required=True, type=Path)
    parser.add_argument("--skip-allowlist", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--environment", default="ci")
    args = parser.parse_args()

    invalid_report = {
        "schema_version": 3,
        "valid": False,
        "errors": ["verification did not complete"],
        "warnings": [],
    }
    try:
        _atomic_write(args.report, invalid_report)
        _safe_path(args.inventory, "inventory")
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        non_pg_inventory, pg_inventory = _validate_inventory(inventory)

        non_pg_results = _parse_junit(args.non_pg_junit, "non_pg")
        pg_results = _parse_junit(args.pg_junit, "pg")
        allowlist = _load_allowlist(args.skip_allowlist)
        now = datetime.now(UTC)

        errors: list[str] = []
        warnings: list[str] = []

        for label, expected, actual in (
            ("non_pg", non_pg_inventory, set(non_pg_results)),
            ("pg", pg_inventory, set(pg_results)),
        ):
            missing = expected - actual
            extra = actual - expected
            if missing:
                errors.append(f"{label}: {len(missing)} missing; first: {sorted(missing)[0]}")
            if extra:
                errors.append(f"{label}: {len(extra)} extra; first: {sorted(extra)[0]}")

        cross_non_pg = set(non_pg_results) & pg_inventory
        cross_pg = set(pg_results) & non_pg_inventory
        if cross_non_pg:
            errors.append(f"non_pg JUnit contains PG nodes: {sorted(cross_non_pg)[0]}")
        if cross_pg:
            errors.append(f"pg JUnit contains non-PG nodes: {sorted(cross_pg)[0]}")

        all_results = {**non_pg_results, **pg_results}
        counts = {
            "inventory_non_pg": len(non_pg_inventory),
            "inventory_pg": len(pg_inventory),
            "executed_non_pg": len(non_pg_results),
            "executed_pg": len(pg_results),
            "passed": 0,
            "failed": 0,
            "error": 0,
            "skipped": 0,
            "xfail": 0,
            "xpass": 0,
            "allowed_skip": 0,
            "unexpected_skip": 0,
        }

        used_allowlist: set[str] = set()
        for node_id, result in sorted(all_results.items()):
            outcome = result["outcome"]
            if outcome == "passed":
                counts["passed"] += 1
            elif outcome == "failed":
                counts["failed"] += 1
            elif outcome == "error":
                counts["error"] += 1
            elif outcome == "xfail":
                counts["xfail"] += 1
            elif outcome == "xpass":
                counts["xpass"] += 1
                errors.append(f"strict xpass: {node_id}")
            elif outcome == "skipped":
                counts["skipped"] += 1
                allowed = False
                for entry in allowlist:
                    pattern = entry["node_id_pattern"]
                    if (
                        _match_pattern(pattern, node_id)
                        and args.environment in entry["environments"]
                    ):
                        expires = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
                        if now <= expires:
                            allowed = True
                            used_allowlist.add(pattern)
                            break
                if allowed:
                    counts["allowed_skip"] += 1
                else:
                    counts["unexpected_skip"] += 1
                    errors.append(f"unexpected skip: {node_id}")

        if counts["failed"]:
            errors.append(f"{counts['failed']} test(s) failed")
        if counts["error"]:
            errors.append(f"{counts['error']} test(s) errored")
        if pg_inventory and not pg_results:
            errors.append("PG inventory nonempty but PG JUnit has zero tests")

        for entry in allowlist:
            pattern = entry["node_id_pattern"]
            expires = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
            if now > expires:
                errors.append(f"expired allowlist entry: {pattern} ({entry['expires_at']})")
            elif pattern not in used_allowlist:
                errors.append(f"unused allowlist entry: {pattern}")

        report = {
            "schema_version": 3,
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "counts": counts,
            "digest": _outcome_digest(non_pg_results, pg_results),
        }
        _atomic_write(args.report, report)

        if errors:
            for error in errors:
                print(f"EXECUTION ERROR: {error}", file=sys.stderr)
            sys.exit(1)

        print(
            f"Execution valid: {counts['executed_non_pg']} non-PG + "
            f"{counts['executed_pg']} PG, {counts['passed']} passed"
        )
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
        invalid_report["errors"] = [str(exc)]
        with contextlib.suppress(ValueError, OSError):
            _atomic_write(args.report, invalid_report)
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
