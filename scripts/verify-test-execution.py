#!/usr/bin/env python3
"""Verify every inventoried selected test has a JUnit execution result.

Requires exact inventory-node == JUnit-executed-node membership per
partition. Rejects extras, missing, duplicates, zero PG, unexpected
skips, strict xpass, and test failures/errors.

Exit 0 on valid execution, 1 on violation, 2 on input/safety error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

_MAX_FILE_BYTES = 100 * 1024 * 1024
_MAX_JUNIT_FILES = 20
_MAX_TESTCASES = 100_000
_MAX_ALLOWLIST_EXPIRY_DAYS = 14


def _safe_path(path: Path, label: str) -> None:
    if not path.is_file():
        print(f"ERROR: {label} not found: {path}", file=sys.stderr)
        sys.exit(2)
    if path.is_symlink():
        print(f"ERROR: {label} is a symlink", file=sys.stderr)
        sys.exit(2)
    if path.stat().st_size > _MAX_FILE_BYTES:
        print(f"ERROR: {label} exceeds size limit", file=sys.stderr)
        sys.exit(2)


def _safe_xml(path: Path, label: str) -> ET.Element:
    _safe_path(path, label)
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        print(f"ERROR: {label} is malformed XML: {exc}", file=sys.stderr)
        sys.exit(2)
    return tree.getroot()


def _classname_to_path(classname: str) -> tuple[str, list[str]]:
    parts = classname.split(".")
    path_parts: list[str] = []
    class_parts: list[str] = []
    for i, part in enumerate(parts):
        if part and part[0].isupper() and i > 0:
            class_parts = parts[i:]
            break
        path_parts.append(part)
    path = "/".join(path_parts) + ".py" if path_parts else ""
    return path, class_parts


def _normalize_node(classname: str, name: str) -> str:
    path, class_parts = _classname_to_path(classname)
    if path and class_parts:
        return f"{path}::{'::'.join(class_parts)}::{name}"
    if path:
        return f"{path}::{name}"
    return name


def _parse_junit(paths: list[Path]) -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    total = 0
    for path in paths:
        root = _safe_xml(path, str(path))
        if root.tag not in ("testsuite", "testsuites"):
            print(
                f"ERROR: {path} root element is {root.tag!r}, expected testsuite or testsuites",
                file=sys.stderr,
            )
            sys.exit(2)
        for tc in root.iter("testcase"):
            total += 1
            if total > _MAX_TESTCASES:
                print(
                    f"ERROR: exceeds {_MAX_TESTCASES} testcases",
                    file=sys.stderr,
                )
                sys.exit(2)
            classname = tc.get("classname", "")
            name = tc.get("name", "")
            if not name:
                continue

            node_prop = None
            props = tc.find("properties")
            if props is not None:
                for prop in props.iter("property"):
                    if prop.get("name") == "node_id":
                        node_prop = prop.get("value")
                        break

            node_id = node_prop or _normalize_node(classname, name)

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
                existing = results[node_id]["outcome"]
                if existing in ("passed", "xfail") and outcome in (
                    "failed",
                    "error",
                    "skipped",
                ):
                    results[node_id] = {
                        "outcome": outcome,
                        "source": str(path),
                    }
            else:
                results[node_id] = {"outcome": outcome, "source": str(path)}
    return results


def _load_allowlist(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    _safe_path(path, "allowlist")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"ERROR: allowlist malformed: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict) or "entries" not in data:
        print("ERROR: allowlist missing 'entries'", file=sys.stderr)
        sys.exit(2)
    entries = data.get("entries", [])
    for i, entry in enumerate(entries):
        required = {
            "node_id_pattern",
            "owner",
            "reason",
            "issue_url",
            "created_at",
            "expires_at",
            "environments",
        }
        if not required.issubset(entry.keys()):
            print(
                f"ERROR: allowlist entry {i} missing required fields "
                f"(need {sorted(required - set(entry.keys()))})",
                file=sys.stderr,
            )
            sys.exit(2)
        pat = entry.get("node_id_pattern", "")
        if not pat or pat == "tests/*" or pat.endswith("/**") or pat == "*":
            print(
                f"ERROR: allowlist entry {i} overbroad pattern: {pat!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        created = entry.get("created_at", "")
        expires = entry.get("expires_at", "")
        if created and expires:
            try:
                c_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                e_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if e_dt > c_dt + timedelta(days=_MAX_ALLOWLIST_EXPIRY_DAYS):
                    print(
                        f"ERROR: allowlist entry {i} expiry exceeds "
                        f"{_MAX_ALLOWLIST_EXPIRY_DAYS} days from creation",
                        file=sys.stderr,
                    )
                    sys.exit(2)
            except (ValueError, TypeError):
                print(
                    f"ERROR: allowlist entry {i} malformed dates",
                    file=sys.stderr,
                )
                sys.exit(2)
    return entries


def _match_pattern(pattern: str, node_id: str) -> bool:
    if "*" not in pattern:
        return pattern == node_id
    regex = re.escape(pattern).replace(r"\*", "[^/]*")
    return bool(re.fullmatch(regex, node_id))


def _is_skip_allowed(
    node_id: str,
    entries: list[dict[str, str]],
    now: datetime,
    env: str,
) -> tuple[bool, str | None]:
    for entry in entries:
        pattern = entry.get("node_id_pattern", "")
        if not _match_pattern(pattern, node_id):
            continue
        envs = entry.get("environments", [])
        if envs and env not in envs:
            continue
        expires = entry.get("expires_at", "")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if now > exp_dt:
                    return False, f"allowed skip expired at {expires}"
            except (ValueError, TypeError):
                return False, f"malformed expires_at: {expires}"
        return True, None
    return False, None


def _outcome_digest(results: dict[str, dict[str, str]]) -> str:
    canonical = "\n".join(f"{nid}:{r['outcome']}" for nid, r in sorted(results.items()))
    return hashlib.sha256(canonical.encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify test execution truth")
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument(
        "--junit",
        required=True,
        action="append",
        type=Path,
        dest="junit_files",
    )
    parser.add_argument("--skip-allowlist", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--environment", default="ci")
    args = parser.parse_args()

    if len(args.junit_files) > _MAX_JUNIT_FILES:
        print(
            f"ERROR: too many JUnit files ({len(args.junit_files)})",
            file=sys.stderr,
        )
        sys.exit(2)

    _safe_path(args.inventory, "inventory")
    try:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"ERROR: inventory malformed: {exc}", file=sys.stderr)
        sys.exit(2)

    if not inventory.get("valid", False):
        print("ERROR: partition inventory is invalid", file=sys.stderr)
        sys.exit(1)

    inv_nodes = inventory.get("nodes", {})
    pg_inventory = set(inv_nodes.get("pg", []))
    non_pg_inventory = set(inv_nodes.get("non_pg", []))
    all_inventory = pg_inventory | non_pg_inventory

    results = _parse_junit(args.junit_files)
    allowlist = _load_allowlist(args.skip_allowlist)
    now = datetime.now(UTC)
    env = args.environment

    errors: list[str] = []
    warnings: list[str] = []

    counts = {
        "inventory_all": len(all_inventory),
        "inventory_pg": len(pg_inventory),
        "inventory_non_pg": len(non_pg_inventory),
        "junit_total": len(results),
        "passed": 0,
        "failed": 0,
        "error": 0,
        "skipped": 0,
        "xfail": 0,
        "xpass": 0,
        "allowed_skip": 0,
        "unexpected_skip": 0,
    }

    for node_id, result in sorted(results.items()):
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
            allowed, reason = _is_skip_allowed(node_id, allowlist, now, env)
            if allowed:
                counts["allowed_skip"] += 1
            else:
                counts["unexpected_skip"] += 1
                msg = f"unexpected skip: {node_id}"
                if reason:
                    msg += f" ({reason})"
                errors.append(msg)

    if counts["failed"] > 0:
        errors.append(f"{counts['failed']} test(s) failed")

    if counts["error"] > 0:
        errors.append(f"{counts['error']} test(s) errored")

    if counts["junit_total"] == 0:
        errors.append("no JUnit test results found")

    executed_nodes = set(results.keys())
    missing_from_junit = all_inventory - executed_nodes
    extra_in_junit = executed_nodes - all_inventory

    if missing_from_junit:
        errors.append(
            f"{len(missing_from_junit)} inventoried nodes missing from "
            f"JUnit; first: {sorted(missing_from_junit)[0]}"
        )

    if extra_in_junit:
        warnings.append(
            f"{len(extra_in_junit)} JUnit nodes not in inventory; "
            f"first: {sorted(extra_in_junit)[0]}"
        )

    pg_executed = sum(
        1 for nid in pg_inventory if nid in results and results[nid]["outcome"] != "skipped"
    )
    if pg_executed == 0 and pg_inventory:
        errors.append(f"pg partition has {len(pg_inventory)} inventoried tests but zero executed")

    for entry in allowlist:
        pattern = entry.get("node_id_pattern", "")
        expires = entry.get("expires_at", "")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if now > exp_dt:
                    errors.append(f"expired allowlist entry: {pattern} ({expires})")
                    continue
            except (ValueError, TypeError):
                errors.append(f"malformed allowlist expires_at: {pattern}")
                continue
        matched = any(
            _match_pattern(pattern, nid) for nid in results if results[nid]["outcome"] == "skipped"
        )
        if not matched:
            warnings.append(f"unused allowlist entry: {pattern}")

    report = {
        "schema_version": 2,
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "counts": counts,
        "membership": {
            "missing_from_junit": len(missing_from_junit),
            "extra_in_junit": len(extra_in_junit),
        },
        "digest": _outcome_digest(results),
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")

    if errors:
        for err in errors:
            print(f"EXECUTION ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    for w in warnings:
        print(f"EXECUTION WARNING: {w}", file=sys.stderr)

    print(
        f"Execution valid: {counts['passed']} passed, "
        f"{counts['failed']} failed, {counts['error']} error, "
        f"{counts['skipped']} skipped ({counts['allowed_skip']} allowed), "
        f"{counts['xfail']} xfail, {counts['xpass']} xpass"
    )


if __name__ == "__main__":
    main()
