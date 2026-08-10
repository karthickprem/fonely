"""Dependency-free orchestrator — run manifest and phase wrapper.

Authority 1: owns run-manifest.json and phase-results.jsonl.
Uses only Python stdlib. Runs immediately after checkout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from ci_evidence.schemas import (
    PHASE_RESULT_SCHEMA,
    PHASE_RESULTS_FILE,
    RUN_MANIFEST_FILE,
    RUN_MANIFEST_SCHEMA,
    VALID_ENVIRONMENTS,
    VALID_PHASES,
)
from ci_evidence.writer import (
    EvidenceWriteError,
    TrustedRoot,
    append_jsonl,
    exclusive_create_empty,
    exclusive_create_json,
    safe_read,
)


def _git_rev(ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", ref],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"ERROR: git rev-parse {ref} failed: {result.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    value = result.stdout.strip()
    if not value or len(value) != 40 or not all(c in "0123456789abcdef" for c in value):
        print(f"ERROR: invalid git ref {ref}: {value!r}", file=sys.stderr)
        raise SystemExit(2)
    return value


def cmd_init(args: argparse.Namespace) -> None:
    root_path = Path(args.evidence_root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)

    source_sha = _git_rev("HEAD")
    source_tree = _git_rev("HEAD^{tree}")

    manifest = {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "workflow_run_id": args.run_id,
        "workflow_attempt": int(args.attempt),
        "environment": args.environment,
        "required_phases": list(VALID_PHASES),
        "created_at_utc": datetime.now(UTC).isoformat(),
    }

    with TrustedRoot(root_path) as root:
        exclusive_create_json(root, RUN_MANIFEST_FILE, manifest)
        exclusive_create_empty(root, PHASE_RESULTS_FILE)

    print(f"Evidence initialized: {source_sha} run={args.run_id}")


def cmd_phase(args: argparse.Namespace) -> None:
    root_path = Path(args.evidence_root).resolve()

    with TrustedRoot(root_path) as root:
        raw = safe_read(root, RUN_MANIFEST_FILE)
        manifest = json.loads(raw)

        if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA:
            print("ERROR: invalid run manifest schema", file=sys.stderr)
            raise SystemExit(2)

        phase_name = args.phase
        if phase_name not in VALID_PHASES:
            print(f"ERROR: unknown phase: {phase_name}", file=sys.stderr)
            raise SystemExit(2)

        results_raw = safe_read(root, PHASE_RESULTS_FILE)
        existing_phases = []
        content = results_raw.decode().strip()
        if content:
            for line in content.splitlines():
                existing_phases.append(json.loads(line)["phase"])

        if phase_name in existing_phases:
            print(f"ERROR: duplicate phase: {phase_name}", file=sys.stderr)
            raise SystemExit(2)

        sequence = len(existing_phases) + 1
        start_utc = datetime.now(UTC).isoformat()

        cmd = args.command
        try:
            result = subprocess.run(cmd, check=False)
            exit_code = result.returncode
        except FileNotFoundError:
            exit_code = 127
        except Exception:
            exit_code = 1

        end_utc = datetime.now(UTC).isoformat()

        if exit_code < 0:
            failure_class = f"signal_{-exit_code}"
        elif exit_code == 0:
            failure_class = None
        elif exit_code == 127:
            failure_class = "command_not_found"
        else:
            failure_class = "nonzero_exit"

        phase_result = {
            "schema_version": PHASE_RESULT_SCHEMA,
            "phase": phase_name,
            "sequence": sequence,
            "exit_code": exit_code,
            "failure_class": failure_class,
            "start_utc": start_utc,
            "end_utc": end_utc,
        }

        append_jsonl(root, PHASE_RESULTS_FILE, phase_result)

    raise SystemExit(exit_code)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ci-evidence-orchestrator")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    init_p = sub.add_parser("init")
    init_p.add_argument("--evidence-root", required=True)
    init_p.add_argument("--run-id", required=True)
    init_p.add_argument("--attempt", required=True)
    init_p.add_argument("--environment", required=True, choices=sorted(VALID_ENVIRONMENTS))

    phase_p = sub.add_parser("phase")
    phase_p.add_argument("--evidence-root", required=True)
    phase_p.add_argument("--phase", required=True)
    phase_p.add_argument("command", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    try:
        if args.subcommand == "init":
            cmd_init(args)
        elif args.subcommand == "phase":
            if not args.command:
                print("ERROR: no command specified", file=sys.stderr)
                raise SystemExit(2)
            if args.command[0] == "--":
                args.command = args.command[1:]
            if not args.command:
                print("ERROR: no command after --", file=sys.stderr)
                raise SystemExit(2)
            cmd_phase(args)
    except EvidenceWriteError as exc:
        print(f"EVIDENCE ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
