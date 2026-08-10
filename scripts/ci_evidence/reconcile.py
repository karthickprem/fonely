"""Reconciler — Authority 3. Validates all evidence and exclusively creates terminal.json.

Reads but never mutates: run manifest, phase results, collection manifests,
event streams, optional JUnit, and waiver file. Creates terminal.json exactly
once via exclusive atomic creation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ci_evidence.schemas import (
    COLLECTION_MANIFEST_SCHEMA,
    EXECUTION_EVENT_SCHEMA,
    MAX_NODES,
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
    VALID_PHASES,
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


def _load_json(root: TrustedRoot, relpath: str) -> tuple[dict[str, Any], bytes]:
    raw = safe_read(root, relpath)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ReconcileError(f"{relpath}: expected JSON object, got {type(data).__name__}")
    return data, raw


def _validate_manifest(root: TrustedRoot, environment: str) -> dict[str, Any]:
    data, _ = _load_json(root, RUN_MANIFEST_FILE)
    if not isinstance(data, dict) or data.get("schema_version") != RUN_MANIFEST_SCHEMA:
        raise ReconcileError("invalid run manifest schema")
    for key in ("source_sha", "source_tree", "workflow_run_id", "environment"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ReconcileError(f"manifest missing {key}")
    if data["environment"] != environment:
        raise ReconcileError(f"CLI environment {environment!r} != manifest {data['environment']!r}")
    required = data.get("required_phases")
    if not isinstance(required, list) or required != list(VALID_PHASES):
        raise ReconcileError("manifest required_phases does not match canonical phase catalogue")
    return data


def _validate_phases(
    root: TrustedRoot, manifest: dict[str, Any]
) -> tuple[list[str], list[str], list[str], dict[str, int]]:
    incomplete: list[str] = []
    evidence: list[str] = []
    test_fail: list[str] = []
    phase_exits: dict[str, int] = {}

    try:
        raw = safe_read(root, PHASE_RESULTS_FILE)
    except EvidenceWriteError:
        required = manifest.get("required_phases", [])
        if required:
            incomplete.append(f"no phase results but {len(required)} required")
        return incomplete, evidence, test_fail, phase_exits

    content = raw.decode().strip()
    if not content:
        required = manifest.get("required_phases", [])
        if required:
            incomplete.append(f"no phase results but {len(required)} required")
        return incomplete, evidence, test_fail, phase_exits

    seen_phases: list[str] = []
    phase_sequences: dict[str, int] = {}
    prev_seq = 0
    for i, line in enumerate(content.splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            evidence.append(f"malformed phase result line {i + 1}")
            continue
        if not isinstance(record, dict):
            evidence.append(f"phase result {i + 1} not an object")
            continue
        if record.get("schema_version") != PHASE_RESULT_SCHEMA:
            evidence.append(f"phase result {i + 1} bad schema")
        phase = record.get("phase", "")
        if not isinstance(phase, str) or not phase:
            evidence.append(f"phase result {i + 1} missing phase name")
            continue
        if phase in seen_phases:
            evidence.append(f"duplicate phase: {phase}")
        seen_phases.append(phase)
        seq = record.get("sequence", 0)
        if not isinstance(seq, int) or seq != prev_seq + 1:
            evidence.append(f"phase sequence gap at {seq}")
        prev_seq = seq
        phase_sequences[phase] = seq
        failure_class = record.get("failure_class")
        exit_code = record.get("exit_code")

        if failure_class == "not_run":
            cause = record.get("cause_phase")
            cause_seq = record.get("cause_sequence")
            if not isinstance(cause, str) or not cause:
                evidence.append(f"phase {phase} not_run missing cause_phase")
            elif cause not in phase_exits:
                evidence.append(f"phase {phase} not_run cause {cause} not recorded")
            elif phase_exits.get(cause, 0) == 0:
                evidence.append(f"phase {phase} not_run cause {cause} did not fail")
            elif cause_seq != phase_sequences.get(cause):
                evidence.append(
                    f"phase {phase} not_run cause_sequence {cause_seq} "
                    f"!= recorded {phase_sequences.get(cause)}"
                )
            if not isinstance(cause_seq, int) or cause_seq >= seq:
                evidence.append(f"phase {phase} not_run cause_sequence not prior")
            continue

        if not isinstance(exit_code, int):
            evidence.append(f"phase {phase} non-integer exit_code")
            continue
        phase_exits[phase] = exit_code
        if record.get("tree_drifted"):
            evidence.append(f"phase {phase} tree drift detected")
        if exit_code != 0:
            test_fail.append(f"phase {phase} exit={exit_code}")

    required = manifest.get("required_phases", [])
    if seen_phases != required:
        missing = [p for p in required if p not in seen_phases]
        extra = [p for p in seen_phases if p not in required]
        if missing:
            incomplete.append(f"missing required phases: {', '.join(missing)}")
        if extra:
            evidence.append(f"unknown/extra phases: {', '.join(extra)}")
        if not missing and not extra and seen_phases != required:
            evidence.append(f"phase order mismatch: observed {seen_phases} != required {required}")

    return incomplete, evidence, test_fail, phase_exits


def _validate_collections(
    root: TrustedRoot,
    manifest: dict[str, Any],
) -> tuple[set[str], set[str], list[str], list[str]]:
    errors: list[str] = []
    inc_errors: list[str] = []
    partitions: dict[str, set[str]] = {}
    pg_requires_call: dict[str, bool] = {}

    for partition in ("all", "non_pg", "pg"):
        try:
            data, _ = _load_json(root, collection_manifest_file(partition))
        except EvidenceWriteError:
            inc_errors.append(f"missing collection manifest: {partition}")
            partitions[partition] = set()
            continue

        if data.get("schema_version") != COLLECTION_MANIFEST_SCHEMA:
            errors.append(f"{partition} manifest bad schema")
            partitions[partition] = set()
            continue

        if data.get("source_sha") != manifest["source_sha"]:
            errors.append(f"{partition} manifest SHA mismatch")

        if data.get("environment") != manifest["environment"]:
            errors.append(f"{partition} manifest environment mismatch")

        if data.get("partition") != partition:
            errors.append(f"{partition} manifest partition field mismatch")

        nodes = data.get("nodes", [])
        if not isinstance(nodes, list):
            errors.append(f"{partition} manifest nodes not a list")
            partitions[partition] = set()
            continue

        if not nodes:
            errors.append(f"{partition} manifest empty")

        if len(nodes) > MAX_NODES:
            errors.append(f"{partition} manifest exceeds {MAX_NODES} nodes")
            partitions[partition] = set()
            continue

        if not all(isinstance(n, str) and n for n in nodes):
            errors.append(f"{partition} manifest contains invalid node IDs")

        declared_count = data.get("node_count")
        if not isinstance(declared_count, int) or declared_count != len(nodes):
            errors.append(f"{partition} manifest node_count={declared_count} != {len(nodes)}")

        node_set = set(nodes)
        if len(nodes) != len(node_set):
            errors.append(f"{partition} manifest has duplicates")

        expected_digest = digest_nodes(nodes)
        if data.get("nodes_digest") != expected_digest:
            errors.append(f"{partition} manifest digest mismatch")

        partitions[partition] = node_set

        rcb = data.get("requires_call_body", {})
        if partition == "pg":
            for node in nodes:
                if not rcb.get(node, True):
                    errors.append(f"pg node missing requires_call_body: {node}")
                pg_requires_call[node] = True
        elif partition == "non_pg":
            for node in nodes:
                if rcb.get(node, False):
                    errors.append(f"non_pg node has requires_call_body: {node}")

    all_nodes = partitions.get("all", set())
    npg = partitions.get("non_pg", set())
    pg = partitions.get("pg", set())

    if npg & pg:
        errors.append("non_pg and pg partitions overlap")
    if npg | pg != all_nodes:
        errors.append("non_pg union pg != all")

    return npg, pg, errors, inc_errors


def _validate_events(
    root: TrustedRoot,
    manifest: dict[str, Any],
    partition: str,
    expected_nodes: set[str],
) -> tuple[dict[str, list[dict[str, Any]]], list[str], list[str], int | None]:
    evidence_errors: list[str] = []
    incomplete_errors: list[str] = []
    node_events: dict[str, list[dict[str, Any]]] = {}

    try:
        raw = safe_read(root, event_stream_file(partition))
    except EvidenceWriteError:
        incomplete_errors.append(f"missing event stream: {partition}")
        return node_events, evidence_errors, incomplete_errors, None

    lines = raw.decode().strip().splitlines()
    if not lines:
        incomplete_errors.append(f"empty event stream: {partition}")
        return node_events, evidence_errors, incomplete_errors, None

    final_line = lines[-1]
    event_lines = lines[:-1]

    try:
        final = json.loads(final_line)
    except json.JSONDecodeError:
        incomplete_errors.append(f"{partition} malformed final record")
        return node_events, evidence_errors, incomplete_errors, None

    if final.get("record_type") != "final":
        incomplete_errors.append(f"{partition} missing final record")
        return node_events, evidence_errors, incomplete_errors, None

    if final.get("schema_version") != EXECUTION_EVENT_SCHEMA:
        evidence_errors.append(f"{partition} final record bad schema_version")

    if final.get("source_sha") != manifest["source_sha"]:
        evidence_errors.append(f"{partition} final record SHA mismatch")

    if final.get("partition") != partition:
        evidence_errors.append(f"{partition} final record partition mismatch")

    if final.get("environment") != manifest["environment"]:
        evidence_errors.append(f"{partition} final record environment mismatch")

    expected_final_seq = len(event_lines) + 1
    if final.get("final_sequence") != expected_final_seq:
        evidence_errors.append(
            f"{partition} final_sequence {final.get('final_sequence')} "
            f"!= expected {expected_final_seq}"
        )

    stream_bytes = "".join(ln + "\n" for ln in event_lines).encode("utf-8")
    if final.get("preceding_stream_digest") != digest_bytes(stream_bytes):
        evidence_errors.append(f"{partition} stream digest mismatch")

    if final.get("selected_nodes_digest") != digest_nodes(expected_nodes):
        evidence_errors.append(f"{partition} selected nodes digest mismatch")

    prev_seq = 0
    for i, line in enumerate(event_lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            evidence_errors.append(f"{partition} event line {i + 1} malformed")
            continue

        if event.get("schema_version") != EXECUTION_EVENT_SCHEMA:
            evidence_errors.append(f"{partition} event {i + 1} bad schema")

        seq = event.get("sequence", 0)
        if seq != prev_seq + 1:
            evidence_errors.append(f"{partition} sequence gap/duplicate at {seq}")
        prev_seq = seq

        node_id = event.get("node_id", "")
        phase = event.get("phase", "")
        outcome = event.get("outcome", "")

        if phase not in VALID_EVENT_PHASES:
            evidence_errors.append(f"{partition} unknown phase: {phase}")
        if outcome not in VALID_EVENT_OUTCOMES:
            evidence_errors.append(f"{partition} unknown outcome: {outcome}")

        if node_id not in expected_nodes:
            evidence_errors.append(f"{partition} extra node: {node_id}")

        node_events.setdefault(node_id, []).append(event)

    missing = expected_nodes - set(node_events)
    if missing:
        incomplete_errors.append(f"{partition} missing events for {len(missing)} nodes")

    pytest_exit = final.get("pytest_exit_status")
    if not isinstance(pytest_exit, int):
        evidence_errors.append(f"{partition} final record missing/invalid pytest_exit_status")
        pytest_exit = None

    return node_events, evidence_errors, incomplete_errors, pytest_exit


def _validate_node_state_machines(
    node_events: dict[str, list[dict[str, Any]]],
    partition: str,
) -> list[str]:
    errors = []
    required_order = ("setup", "call", "teardown")

    for node_id, events in node_events.items():
        phases_seen = [e.get("phase") for e in events]

        for phase in phases_seen:
            if phase not in required_order:
                errors.append(f"{partition} unknown phase {phase}: {node_id}")

        if len(phases_seen) != len(set(phases_seen)):
            errors.append(f"{partition} duplicate phases: {node_id}")
            continue

        ordered = [p for p in phases_seen if p in required_order]
        order_indices = [required_order.index(p) for p in ordered]
        if order_indices != sorted(order_indices):
            errors.append(f"{partition} invalid phase order {ordered}: {node_id}")
            continue

        if not phases_seen:
            errors.append(f"{partition} no phases: {node_id}")
            continue

        phase_map = {e.get("phase"): e for e in events}

        setup = phase_map.get("setup")
        call = phase_map.get("call")
        teardown = phase_map.get("teardown")

        if not setup:
            errors.append(f"{partition} missing setup: {node_id}")
            continue

        setup_outcome = setup["outcome"]
        setup_xfail = setup.get("wasxfail")

        if setup_outcome == "skipped":
            if setup_xfail:
                errors.append(f"xfail requires waiver: {node_id}")
            elif partition == "non_pg":
                errors.append(f"non_pg skip not waivable: {node_id}")
            if call:
                errors.append(f"{partition} call after setup skip: {node_id}")
            continue

        if setup_outcome in ("failed", "error"):
            errors.append(f"{partition} setup {setup_outcome}: {node_id}")
            continue

        if not call:
            errors.append(f"{partition} missing call after setup: {node_id}")
            continue

        call_outcome = call["outcome"]
        call_xfail = call.get("wasxfail")

        if call_xfail and call_outcome == "passed":
            errors.append(f"XPASS: {node_id}")
        elif call_xfail:
            errors.append(f"xfail requires waiver: {node_id}")
        elif call_outcome in ("failed", "error"):
            errors.append(f"{partition} call {call_outcome}: {node_id}")
        elif call_outcome == "skipped" and partition == "non_pg":
            errors.append(f"non_pg skip not waivable: {node_id}")

        if not teardown:
            errors.append(f"{partition} missing teardown: {node_id}")
            continue

        td_outcome = teardown["outcome"]
        if td_outcome in ("failed", "error", "skipped"):
            errors.append(f"{partition} teardown {td_outcome}: {node_id}")

    return errors


def _validate_pg_proof(
    pg_events: dict[str, list[dict[str, Any]]],
    pg_nodes: set[str],
    waived_nodes: dict[str, str],
) -> list[str]:
    errors = []
    for node_id in pg_nodes:
        events = pg_events.get(node_id, [])
        phase_map = {e.get("phase"): e for e in events}
        setup = phase_map.get("setup")
        call = phase_map.get("call")
        waiver_class = waived_nodes.get(node_id)

        if waiver_class == "setup_skip":
            if not setup or setup["outcome"] != "skipped":
                errors.append(f"setup_skip waiver but setup not skipped: {node_id}")
            continue

        if waiver_class == "call_xfail":
            if not call or not call.get("wasxfail"):
                errors.append(f"call_xfail waiver but call not xfail: {node_id}")
            continue

        if not call:
            errors.append(f"PG node missing call evidence: {node_id}")
            continue
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
) -> tuple[dict[str, str], list[str]]:
    errors = []
    waived: dict[str, str] = {}

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

        if "*" in node_id or "?" in node_id:
            errors.append(f"waiver entry {i} glob pattern not allowed")
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
            waived[node_id] = exc_class

    return waived, errors


def _classify_terminal(
    phase_errors: list[str],
    collection_errors: list[str],
    incomplete_errors: list[str],
    evidence_errors: list[str],
    test_errors: list[str],
) -> str:
    has_evidence = bool(collection_errors or evidence_errors)
    has_incomplete = bool(incomplete_errors)
    has_test = bool(test_errors or phase_errors)

    if has_evidence:
        return TERMINAL_EVIDENCE_FAILED

    if has_incomplete:
        return TERMINAL_INCOMPLETE

    if has_test:
        return TERMINAL_TEST_FAILED

    return TERMINAL_SUCCESS


def reconcile(
    evidence_root: str,
    waivers_path: str,
    environment: str,
) -> dict[str, Any]:
    root_path = Path(evidence_root).resolve()

    with TrustedRoot(root_path) as root:
        try:
            return _reconcile_inner(root, waivers_path, environment)
        except (
            ReconcileError,
            EvidenceWriteError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
        ) as exc:
            state = TERMINAL_EVIDENCE_FAILED
            exc_str = str(exc).lower()
            if isinstance(exc, (EvidenceWriteError, ReconcileError)) and (
                "not found" in exc_str or "missing" in exc_str
            ):
                state = TERMINAL_INCOMPLETE
            error_msg = f"{type(exc).__name__}: {exc}"
            terminal = {
                "schema_version": TERMINAL_SCHEMA,
                "state": state,
                "source_sha": None,
                "workflow_run_id": None,
                "environment": environment,
                "errors": [error_msg],
                "counts": {},
                "artifact_hashes": {},
                "reconciled_at_utc": datetime.now(UTC).isoformat(),
            }
            try:
                manifest_raw = safe_read(root, RUN_MANIFEST_FILE)
                manifest_data = json.loads(manifest_raw)
                if isinstance(manifest_data, dict):
                    sha = manifest_data.get("source_sha")
                    run_id = manifest_data.get("workflow_run_id")
                    if isinstance(sha, str) and len(sha) == 40:
                        terminal["source_sha"] = sha
                    if isinstance(run_id, str) and run_id:
                        terminal["workflow_run_id"] = run_id
            except Exception:
                pass
            try:
                return _write_terminal(root, terminal)
            except EvidenceWriteError:
                return terminal


def _reconcile_inner(
    root: TrustedRoot,
    waivers_path: str,
    environment: str,
) -> dict[str, Any]:
    manifest = _validate_manifest(root, environment)

    phase_inc, phase_ev, phase_test, phase_exits = _validate_phases(root, manifest)

    npg_nodes, pg_nodes, collection_errors, collection_inc = _validate_collections(root, manifest)

    npg_events, npg_ev_errors, npg_inc_errors, npg_pytest_exit = _validate_events(
        root, manifest, "non_pg", npg_nodes
    )
    pg_events, pg_ev_errors, pg_inc_errors, pg_pytest_exit = _validate_events(
        root, manifest, "pg", pg_nodes
    )
    evidence_errors = npg_ev_errors + pg_ev_errors
    incomplete_errors = collection_inc + npg_inc_errors + pg_inc_errors

    test_errors: list[str] = []
    for label, pytest_exit, phase_name in [
        ("non_pg", npg_pytest_exit, "test_non_pg"),
        ("pg", pg_pytest_exit, "test_pg"),
    ]:
        if pytest_exit is not None and pytest_exit != 0:
            test_errors.append(f"{label} pytest_exit_status={pytest_exit}")
        if (
            pytest_exit is not None
            and phase_name in phase_exits
            and (pytest_exit == 0) != (phase_exits[phase_name] == 0)
        ):
            evidence_errors.append(
                f"{label} pytest_exit_status={pytest_exit} contradicts "
                f"phase {phase_name} exit={phase_exits[phase_name]}"
            )

    if not npg_nodes and not pg_nodes:
        incomplete_errors.append("no selected nodes in either partition")

    state_errors: list[str] = []
    pg_errors: list[str] = []
    waiver_errors: list[str] = []
    waived: dict[str, str] = {}

    if not incomplete_errors:
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
    else:
        now = datetime.now(UTC)

    for err in state_errors:
        if "XPASS" in err or "failed" in err or "error" in err:
            test_errors.append(err)
        else:
            evidence_errors.append(err)

    evidence_errors.extend(pg_errors)
    evidence_errors.extend(waiver_errors)

    incomplete_errors.extend(phase_inc)
    evidence_errors.extend(phase_ev)
    test_errors.extend(phase_test)

    try:
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        actual_head = head_result.stdout.strip()
        if actual_head == manifest["source_sha"]:
            tree_result = subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if tree_result.stdout.strip() != manifest["source_tree"]:
                evidence_errors.append("final tree != manifest source_tree")
            if manifest.get("environment") == "ci":
                dirty = subprocess.run(
                    ["git", "diff", "--quiet", "HEAD", "--"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                if dirty.returncode != 0:
                    evidence_errors.append("final tracked worktree is dirty")
        elif manifest.get("environment") == "ci":
            evidence_errors.append(f"final HEAD {actual_head} != manifest {manifest['source_sha']}")
    except (OSError, subprocess.TimeoutExpired):
        if manifest.get("environment") == "ci":
            evidence_errors.append("final repo state verification failed")

    terminal_state = _classify_terminal(
        [],
        collection_errors,
        incomplete_errors,
        evidence_errors,
        test_errors,
    )

    all_errors = collection_errors + incomplete_errors + evidence_errors + test_errors

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
