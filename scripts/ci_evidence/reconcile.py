"""Reconciler — Authority 3. Validates all evidence and exclusively creates terminal.json.

Reads but never mutates: run manifest, phase results, collection manifests,
event streams, optional JUnit, and waiver file. Creates terminal.json exactly
once via exclusive atomic creation.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ci_evidence.schemas import (
    COLLECTION_MANIFEST_SCHEMA,
    EXECUTION_EVENT_SCHEMA,
    MAX_WAIVER_LIFETIME_DAYS,
    PHASE_RESULT_SCHEMA,
    PHASE_RESULTS_FILE,
    RUN_MANIFEST_FILE,
    RUN_MANIFEST_SCHEMA,
    TERMINAL_EVIDENCE_FAILED,
    TERMINAL_FILE,
    TERMINAL_INCOMPLETE,
    TERMINAL_SCHEMA,
    TERMINAL_SUCCESS,
    TERMINAL_TEST_FAILED,
    VALID_ENVIRONMENTS,
    VALID_EVENT_OUTCOMES,
    VALID_EVENT_PHASES,
    WAIVER_EXCEPTION_CLASSES,
    WAIVER_SCHEMA,
    collection_manifest_file,
    digest_bytes,
    digest_nodes,
    event_stream_file,
)
from ci_evidence.writer import EvidenceWriteError, TrustedRoot, exclusive_create_json, safe_read


class ReconcileError(Exception):
    pass


def _load_json(root: TrustedRoot, relpath: str) -> Any:
    raw = safe_read(root, relpath)
    return json.loads(raw), raw


def _validate_manifest(root: TrustedRoot) -> dict[str, Any]:
    data, _ = _load_json(root, RUN_MANIFEST_FILE)
    if not isinstance(data, dict) or data.get("schema_version") != RUN_MANIFEST_SCHEMA:
        raise ReconcileError("invalid run manifest schema")
    for key in ("source_sha", "source_tree", "workflow_run_id", "environment"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ReconcileError(f"manifest missing {key}")
    return data


def _validate_phases(root: TrustedRoot, manifest: dict[str, Any]) -> list[str]:
    errors = []
    results_path = root.path / PHASE_RESULTS_FILE
    if not results_path.exists() or results_path.stat().st_size == 0:
        required = manifest.get("required_phases", [])
        if required:
            errors.append(f"no phase results but {len(required)} required")
        return errors

    raw = results_path.read_bytes()
    seen_phases = []
    prev_seq = 0
    for i, line in enumerate(raw.decode().strip().splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"malformed phase result line {i + 1}")
            continue
        if record.get("schema_version") != PHASE_RESULT_SCHEMA:
            errors.append(f"phase result {i + 1} bad schema")
        phase = record.get("phase", "")
        if phase in seen_phases:
            errors.append(f"duplicate phase: {phase}")
        seen_phases.append(phase)
        seq = record.get("sequence", 0)
        if seq != prev_seq + 1:
            errors.append(f"phase sequence gap at {seq}")
        prev_seq = seq
        if record.get("exit_code", 0) != 0:
            errors.append(f"phase {phase} exit={record['exit_code']}")

    required = manifest.get("required_phases", [])
    missing = [p for p in required if p not in seen_phases]
    if missing:
        errors.append(f"missing required phases: {', '.join(missing)}")

    return errors


def _validate_collections(
    root: TrustedRoot,
    manifest: dict[str, Any],
) -> tuple[set[str], set[str], list[str]]:
    errors = []
    partitions: dict[str, set[str]] = {}
    pg_requires_call: dict[str, bool] = {}

    for partition in ("all", "non_pg", "pg"):
        try:
            data, _ = _load_json(root, collection_manifest_file(partition))
        except EvidenceWriteError:
            errors.append(f"missing collection manifest: {partition}")
            partitions[partition] = set()
            continue

        if data.get("schema_version") != COLLECTION_MANIFEST_SCHEMA:
            errors.append(f"{partition} manifest bad schema")
            partitions[partition] = set()
            continue

        if data.get("source_sha") != manifest["source_sha"]:
            errors.append(f"{partition} manifest SHA mismatch")

        nodes = data.get("nodes", [])
        if not nodes:
            errors.append(f"{partition} manifest empty")

        node_set = set(nodes)
        if len(nodes) != len(node_set):
            errors.append(f"{partition} manifest has duplicates")

        expected_digest = digest_nodes(nodes)
        if data.get("nodes_digest") != expected_digest:
            errors.append(f"{partition} manifest digest mismatch")

        partitions[partition] = node_set

        if partition == "pg":
            rcb = data.get("requires_call_body", {})
            for node in nodes:
                pg_requires_call[node] = rcb.get(node, True)

    all_nodes = partitions.get("all", set())
    npg = partitions.get("non_pg", set())
    pg = partitions.get("pg", set())

    if npg & pg:
        errors.append("non_pg and pg partitions overlap")
    if npg | pg != all_nodes:
        errors.append("non_pg union pg != all")

    return npg, pg, errors


def _validate_events(
    root: TrustedRoot,
    manifest: dict[str, Any],
    partition: str,
    expected_nodes: set[str],
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    errors = []
    node_events: dict[str, list[dict[str, Any]]] = {}

    try:
        raw = safe_read(root, event_stream_file(partition))
    except EvidenceWriteError:
        errors.append(f"missing event stream: {partition}")
        return node_events, errors

    lines = raw.decode().strip().splitlines()
    if not lines:
        errors.append(f"empty event stream: {partition}")
        return node_events, errors

    final_line = lines[-1]
    event_lines = lines[:-1]

    try:
        final = json.loads(final_line)
    except json.JSONDecodeError:
        errors.append(f"{partition} malformed final record")
        return node_events, errors

    if final.get("record_type") != "final":
        errors.append(f"{partition} missing final record")
        return node_events, errors

    if final.get("source_sha") != manifest["source_sha"]:
        errors.append(f"{partition} final record SHA mismatch")

    if final.get("partition") != partition:
        errors.append(f"{partition} final record partition mismatch")

    if final.get("environment") != manifest["environment"]:
        errors.append(f"{partition} final record environment mismatch")

    expected_final_seq = len(event_lines) + 1
    if final.get("final_sequence") != expected_final_seq:
        errors.append(
            f"{partition} final_sequence {final.get('final_sequence')} "
            f"!= expected {expected_final_seq}"
        )

    stream_bytes = "".join(ln + "\n" for ln in event_lines).encode("utf-8")
    if final.get("preceding_stream_digest") != digest_bytes(stream_bytes):
        errors.append(f"{partition} stream digest mismatch")

    if final.get("selected_nodes_digest") != digest_nodes(expected_nodes):
        errors.append(f"{partition} selected nodes digest mismatch")

    prev_seq = 0
    for i, line in enumerate(event_lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"{partition} event line {i + 1} malformed")
            continue

        if event.get("schema_version") != EXECUTION_EVENT_SCHEMA:
            errors.append(f"{partition} event {i + 1} bad schema")

        seq = event.get("sequence", 0)
        if seq != prev_seq + 1:
            errors.append(f"{partition} sequence gap/duplicate at {seq}")
        prev_seq = seq

        node_id = event.get("node_id", "")
        phase = event.get("phase", "")
        outcome = event.get("outcome", "")

        if phase not in VALID_EVENT_PHASES:
            errors.append(f"{partition} unknown phase: {phase}")
        if outcome not in VALID_EVENT_OUTCOMES:
            errors.append(f"{partition} unknown outcome: {outcome}")

        if node_id not in expected_nodes:
            errors.append(f"{partition} extra node: {node_id}")

        node_events.setdefault(node_id, []).append(event)

    missing = expected_nodes - set(node_events)
    if missing:
        errors.append(f"{partition} missing events for {len(missing)} nodes")

    return node_events, errors


def _validate_node_state_machines(
    node_events: dict[str, list[dict[str, Any]]],
    partition: str,
) -> list[str]:
    errors = []
    for node_id, events in node_events.items():
        phases_seen = []
        for event in events:
            phase = event.get("phase", "")
            outcome = event.get("outcome", "")
            wasxfail = event.get("wasxfail")

            if phase in phases_seen:
                errors.append(f"{partition} duplicate phase {phase}: {node_id}")
            phases_seen.append(phase)

            if wasxfail and outcome == "passed":
                errors.append(f"XPASS: {node_id}")
            elif wasxfail:
                errors.append(f"xfail requires waiver: {node_id}")

            if outcome in ("failed", "error"):
                errors.append(f"{partition} {outcome}: {node_id}")

            if outcome == "skipped" and not wasxfail and partition == "non_pg":
                errors.append(f"non_pg skip not waivable: {node_id}")

    return errors


def _validate_pg_proof(
    pg_events: dict[str, list[dict[str, Any]]],
    pg_nodes: set[str],
    waived_nodes: set[str],
) -> list[str]:
    errors = []
    for node_id in pg_nodes:
        if node_id in waived_nodes:
            continue
        events = pg_events.get(node_id, [])
        call_events = [e for e in events if e.get("phase") == "call"]
        if not call_events:
            errors.append(f"PG node missing call evidence: {node_id}")
            continue
        call = call_events[0]
        if call["outcome"] == "skipped":
            errors.append(f"PG node call skipped without waiver: {node_id}")
        elif call["outcome"] != "passed":
            pass
    return errors


def _validate_waivers(
    waiver_path: Path,
    environment: str,
    pg_nodes: set[str],
    now: datetime,
) -> tuple[set[str], list[str]]:
    errors = []
    waived: set[str] = set()

    try:
        data = json.loads(waiver_path.read_bytes())
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"waiver file error: {type(exc).__name__}")
        return waived, errors

    if not isinstance(data, dict) or data.get("schema_version") != WAIVER_SCHEMA:
        errors.append("waiver schema must be 1")
        return waived, errors

    entries = data.get("entries", [])
    if not isinstance(entries, list):
        errors.append("waiver entries must be array")
        return waived, errors

    seen: list[tuple[str, frozenset[str]]] = []
    required_keys = {
        "node_id",
        "exception_class",
        "owner",
        "reason",
        "issue_url",
        "created_at",
        "expires_at",
        "environments",
    }

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != required_keys:
            errors.append(f"waiver entry {i} invalid fields")
            continue

        node_id = entry.get("node_id", "")
        exc_class = entry.get("exception_class", "")

        if not isinstance(node_id, str) or not node_id:
            errors.append(f"waiver entry {i} invalid node_id")
            continue

        if "*" in node_id or "?" in node_id or "[" in node_id:
            errors.append(f"waiver entry {i} pattern not allowed")
            continue

        if node_id not in pg_nodes:
            errors.append(f"waiver entry {i} non-PG node: {node_id}")
            continue

        if exc_class not in WAIVER_EXCEPTION_CLASSES:
            errors.append(f"waiver entry {i} invalid exception_class")
            continue

        for key in ("owner", "reason"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                errors.append(f"waiver entry {i} blank {key}")

        url = entry.get("issue_url", "")
        parsed = urlparse(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.path in ("", "/")
        ):
            errors.append(f"waiver entry {i} invalid URL")

        envs = entry.get("environments", [])
        if not isinstance(envs, list) or not envs or not set(envs).issubset(VALID_ENVIRONMENTS):
            errors.append(f"waiver entry {i} invalid environments")
            continue

        try:
            created = datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))
            expires = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            errors.append(f"waiver entry {i} invalid timestamps")
            continue

        if created.tzinfo is None or expires.tzinfo is None:
            errors.append(f"waiver entry {i} naive timestamps")
            continue

        if created > now:
            errors.append(f"waiver entry {i} future created_at")

        if expires < created:
            errors.append(f"waiver entry {i} expires before created")

        if expires > created + timedelta(days=MAX_WAIVER_LIFETIME_DAYS):
            errors.append(f"waiver entry {i} lifetime exceeds {MAX_WAIVER_LIFETIME_DAYS} days")

        if now > expires:
            errors.append(f"waiver entry {i} expired")

        identity = (node_id, frozenset(envs))
        if any(node_id == old_node and identity[1] & old_envs for old_node, old_envs in seen):
            errors.append(f"waiver entry {i} duplicate coverage")
        seen.append(identity)

        if environment in envs:
            waived.add(node_id)

    return waived, errors


def _classify_terminal(
    phase_errors: list[str],
    collection_errors: list[str],
    event_errors: list[str],
    state_errors: list[str],
    pg_errors: list[str],
    waiver_errors: list[str],
    has_test_failure: bool,
    is_incomplete: bool,
) -> str:
    if is_incomplete:
        return TERMINAL_INCOMPLETE

    evidence_errors = collection_errors + waiver_errors
    for err in event_errors + state_errors + pg_errors:
        if "XPASS" in err or "failed" in err or "error" in err:
            has_test_failure = True
        else:
            evidence_errors.append(err)

    if evidence_errors:
        return TERMINAL_EVIDENCE_FAILED

    if has_test_failure or phase_errors:
        return TERMINAL_TEST_FAILED

    return TERMINAL_SUCCESS


def reconcile(
    evidence_root: str,
    waivers_path: str,
    environment: str,
) -> dict[str, Any]:
    root_path = Path(evidence_root).resolve()
    is_incomplete = False

    with TrustedRoot(root_path) as root:
        try:
            manifest = _validate_manifest(root)
        except (ReconcileError, EvidenceWriteError) as exc:
            return _write_terminal(
                root,
                {
                    "state": TERMINAL_INCOMPLETE,
                    "errors": [str(exc)],
                },
            )

        phase_errors = _validate_phases(root, manifest)

        npg_nodes, pg_nodes, collection_errors = _validate_collections(root, manifest)

        npg_events, npg_event_errors = _validate_events(root, manifest, "non_pg", npg_nodes)
        pg_events, pg_event_errors = _validate_events(root, manifest, "pg", pg_nodes)
        event_errors = npg_event_errors + pg_event_errors

        if not npg_nodes and not pg_nodes:
            is_incomplete = True

        npg_state_errors = _validate_node_state_machines(npg_events, "non_pg")
        pg_state_errors = _validate_node_state_machines(pg_events, "pg")
        state_errors = npg_state_errors + pg_state_errors

        now = datetime.now(UTC)
        waived, waiver_errors = _validate_waivers(
            Path(waivers_path),
            environment,
            pg_nodes,
            now,
        )

        pg_errors = _validate_pg_proof(pg_events, pg_nodes, waived)

        for wnode in waived:
            events = pg_events.get(wnode, [])
            call_events = [e for e in events if e.get("phase") == "call"]
            call_passed = call_events and call_events[0]["outcome"] == "passed"
            if call_passed and not call_events[0].get("wasxfail"):
                waiver_errors.append(f"unused waiver (node passed): {wnode}")

        has_test_failure = any("failed" in e or "error" in e or "XPASS" in e for e in state_errors)
        has_test_failure = has_test_failure or any("exit=" in e for e in phase_errors)

        terminal_state = _classify_terminal(
            phase_errors,
            collection_errors,
            event_errors,
            state_errors,
            pg_errors,
            waiver_errors,
            has_test_failure,
            is_incomplete,
        )

        all_errors = (
            phase_errors
            + collection_errors
            + event_errors
            + state_errors
            + pg_errors
            + waiver_errors
        )

        artifact_hashes = {}
        for relpath in [
            RUN_MANIFEST_FILE,
            PHASE_RESULTS_FILE,
            collection_manifest_file("all"),
            collection_manifest_file("non_pg"),
            collection_manifest_file("pg"),
            event_stream_file("non_pg"),
            event_stream_file("pg"),
        ]:
            try:
                raw = safe_read(root, relpath)
                artifact_hashes[relpath] = digest_bytes(raw)
            except EvidenceWriteError:
                artifact_hashes[relpath] = None

        terminal = {
            "schema_version": TERMINAL_SCHEMA,
            "state": terminal_state,
            "source_sha": manifest.get("source_sha"),
            "workflow_run_id": manifest.get("workflow_run_id"),
            "environment": environment,
            "errors": all_errors,
            "counts": {
                "non_pg_selected": len(npg_nodes),
                "pg_selected": len(pg_nodes),
                "pg_waived": len(waived),
            },
            "artifact_hashes": artifact_hashes,
            "reconciled_at_utc": now.isoformat(),
        }

        return _write_terminal(root, terminal)


def _write_terminal(root: TrustedRoot, terminal: dict[str, Any]) -> dict[str, Any]:
    exclusive_create_json(root, TERMINAL_FILE, terminal)
    return terminal


def main() -> None:
    parser = argparse.ArgumentParser(prog="ci-evidence-reconcile")
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--waivers", required=True)
    parser.add_argument("--environment", required=True, choices=sorted(VALID_ENVIRONMENTS))
    args = parser.parse_args()

    try:
        terminal = reconcile(args.evidence_root, args.waivers, args.environment)
    except EvidenceWriteError as exc:
        print(f"RECONCILE ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    state = terminal.get("state", "unknown")
    errors = terminal.get("errors", [])

    if errors:
        for err in errors:
            print(f"RECONCILE: {err}", file=sys.stderr)

    if state == TERMINAL_SUCCESS:
        print(f"Terminal: {state}")
    else:
        print(f"Terminal: {state}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
