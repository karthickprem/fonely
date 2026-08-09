#!/usr/bin/env python3
"""Verify pytest marker partitions cover the full test collection exactly.

Parses pytest ``--collect-only -q`` output to extract node IDs and proves
the marker-partitioned runs cover every collected test exactly once.

Exit 0 on valid partition, 1 on violation, 2 on input/safety error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

_NODE_RE = re.compile(r"^tests/\S+\.py::.+$")
_FOOTER_RE = re.compile(r"^\d+(?:/\d+)?\s+tests?\s+collected")
_ERROR_RE = re.compile(r"^={3,}\s*(ERRORS|ERROR collecting)", re.IGNORECASE)
_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_NODES = 100_000


def _safe_path(path: Path, label: str) -> None:
    if not path.is_file():
        print(f"ERROR: {label} file not found: {path}", file=sys.stderr)
        sys.exit(2)
    if path.is_symlink():
        print(f"ERROR: {label} is a symlink", file=sys.stderr)
        sys.exit(2)
    if path.stat().st_size > _MAX_FILE_BYTES:
        print(f"ERROR: {label} exceeds {_MAX_FILE_BYTES} byte limit", file=sys.stderr)
        sys.exit(2)
    try:
        resolved = path.resolve()
        if ".." in str(resolved):
            raise ValueError
    except (ValueError, OSError):
        print(f"ERROR: {label} path traversal rejected", file=sys.stderr)
        sys.exit(2)


def _parse_collection(path: Path, label: str) -> tuple[list[str], int, int, bool]:
    _safe_path(path, label)
    text = path.read_text(encoding="utf-8", errors="replace")
    nodes: list[str] = []
    collected = 0
    deselected = 0
    has_footer = False
    has_error = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _ERROR_RE.match(stripped):
            has_error = True
            continue
        if _NODE_RE.match(stripped):
            if len(nodes) >= _MAX_NODES:
                print(f"ERROR: {label} exceeds {_MAX_NODES} node limit", file=sys.stderr)
                sys.exit(2)
            nodes.append(stripped)
            continue
        m = _FOOTER_RE.match(stripped)
        if m:
            has_footer = True
            nums = re.findall(r"\d+", stripped)
            if nums:
                collected = int(nums[0])
            if "deselected" in stripped and len(nums) >= 2:
                deselected = int(nums[-1])

    return nodes, collected, deselected, has_footer and not has_error


def _content_digest(nodes: frozenset[str]) -> str:
    canonical = "\n".join(sorted(nodes))
    return hashlib.sha256(canonical.encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify test partitions")
    parser.add_argument("--all", required=True, dest="all_file", type=Path)
    parser.add_argument("--non-pg", required=True, dest="non_pg_file", type=Path)
    parser.add_argument("--pg", required=True, dest="pg_file", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    all_nodes, all_collected, _, all_ok = _parse_collection(args.all_file, "all")
    non_pg_nodes, npg_collected, npg_desel, npg_ok = _parse_collection(args.non_pg_file, "non-pg")
    pg_nodes, pg_collected, pg_desel, pg_ok = _parse_collection(args.pg_file, "pg")

    errors: list[str] = []

    if not all_ok:
        errors.append("all collection failed or has errors; check pytest output")
    if not npg_ok:
        errors.append("non-pg collection failed or has errors")
    if not pg_ok:
        errors.append("pg collection failed or has errors")

    all_set = frozenset(all_nodes)
    non_pg_set = frozenset(non_pg_nodes)
    pg_set = frozenset(pg_nodes)

    if len(all_nodes) != len(all_set):
        dupes = len(all_nodes) - len(all_set)
        errors.append(f"all collection has {dupes} duplicate node IDs")

    if len(non_pg_nodes) != len(non_pg_set):
        dupes = len(non_pg_nodes) - len(non_pg_set)
        errors.append(f"non-pg partition has {dupes} duplicate node IDs")

    if len(pg_nodes) != len(pg_set):
        dupes = len(pg_nodes) - len(pg_set)
        errors.append(f"pg partition has {dupes} duplicate node IDs")

    overlap = non_pg_set & pg_set
    if overlap:
        errors.append(f"partitions overlap on {len(overlap)} nodes; first: {sorted(overlap)[0]}")

    union = non_pg_set | pg_set
    missing = all_set - union
    extra = union - all_set

    if missing:
        errors.append(
            f"{len(missing)} nodes in all but missing from partitions; first: {sorted(missing)[0]}"
        )

    if extra:
        errors.append(f"{len(extra)} nodes in partitions but not in all; first: {sorted(extra)[0]}")

    if not pg_set:
        errors.append("pg partition is empty")

    if not non_pg_set:
        errors.append("non-pg partition is empty")

    if all_collected and len(all_nodes) != all_collected:
        errors.append(f"parsed {len(all_nodes)} node IDs but footer says {all_collected}")

    report = {
        "schema_version": 1,
        "valid": len(errors) == 0,
        "errors": errors,
        "counts": {
            "all": len(all_set),
            "non_pg": len(non_pg_set),
            "pg": len(pg_set),
            "overlap": len(overlap),
            "missing": len(missing),
            "extra": len(extra),
        },
        "footer": {
            "all_collected": all_collected,
            "non_pg_collected": npg_collected,
            "non_pg_deselected": npg_desel,
            "pg_collected": pg_collected,
            "pg_deselected": pg_desel,
        },
        "digests": {
            "all": _content_digest(all_set),
            "non_pg": _content_digest(non_pg_set),
            "pg": _content_digest(pg_set),
        },
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")

    if errors:
        for err in errors:
            print(f"PARTITION ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    print(f"Partition valid: {len(all_set)} total = {len(non_pg_set)} non-pg + {len(pg_set)} pg")


if __name__ == "__main__":
    main()
