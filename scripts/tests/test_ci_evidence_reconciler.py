"""Tests for the reconciler and terminal creator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ci_evidence.reconcile import reconcile
from ci_evidence.schemas import (
    PHASE_RESULTS_FILE,
    RUN_MANIFEST_FILE,
    TERMINAL_EVIDENCE_FAILED,
    TERMINAL_FILE,
    TERMINAL_INCOMPLETE,
    TERMINAL_SUCCESS,
    TERMINAL_TEST_FAILED,
    collection_manifest_file,
    digest_bytes,
    digest_nodes,
    event_stream_file,
)
from ci_evidence.writer import EvidenceWriteError, TrustedRoot, exclusive_create_json


def _setup_full_evidence(evidence: Path, waivers: Path) -> None:
    """Create minimal valid evidence for a successful reconciliation."""
    sha = "a" * 40

    with TrustedRoot(evidence) as root:
        exclusive_create_json(
            root,
            RUN_MANIFEST_FILE,
            {
                "schema_version": 1,
                "source_sha": sha,
                "source_tree": "b" * 40,
                "workflow_run_id": "1",
                "workflow_attempt": 1,
                "environment": "ci",
                "required_phases": ["lint"],
                "created_at_utc": "2026-08-10T00:00:00+00:00",
            },
        )

    (evidence / PHASE_RESULTS_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "lint",
                "sequence": 1,
                "exit_code": 0,
                "failure_class": None,
                "start_utc": "2026-08-10T00:00:00+00:00",
                "end_utc": "2026-08-10T00:00:01+00:00",
            }
        )
        + "\n"
    )

    npg_nodes = ["tests/test_a.py::test_a"]
    pg_nodes = ["tests/integration/postgres/test_b.py::test_b"]
    all_nodes = npg_nodes + pg_nodes

    for partition, nodes in [("all", all_nodes), ("non_pg", npg_nodes), ("pg", pg_nodes)]:
        rcb = {n: "postgres" in n for n in nodes}
        (evidence / collection_manifest_file(partition)).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_sha": sha,
                    "environment": "ci",
                    "partition": partition,
                    "nodes": nodes,
                    "requires_call_body": rcb,
                    "node_count": len(nodes),
                    "nodes_digest": digest_nodes(nodes),
                }
            )
            + "\n"
        )

    for partition, nodes in [("non_pg", npg_nodes), ("pg", pg_nodes)]:
        events_data = b""
        seq = 0
        for node in nodes:
            for phase in ("setup", "call", "teardown"):
                seq += 1
                event = {
                    "schema_version": 1,
                    "node_id": node,
                    "sequence": seq,
                    "phase": phase,
                    "outcome": "passed",
                    "wasxfail": None,
                }
                events_data += (json.dumps(event, sort_keys=True) + "\n").encode()

        seq += 1
        final = {
            "schema_version": 1,
            "record_type": "final",
            "partition": partition,
            "source_sha": sha,
            "environment": "ci",
            "final_sequence": seq,
            "selected_nodes_digest": digest_nodes(nodes),
            "preceding_stream_digest": digest_bytes(events_data),
            "pytest_exit_status": 0,
        }
        full_stream = events_data + (json.dumps(final, sort_keys=True) + "\n").encode()
        (evidence / event_stream_file(partition)).write_bytes(full_stream)

    waivers.write_text(json.dumps({"schema_version": 1, "entries": []}) + "\n")


class TestSuccess:
    def test_complete_success(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        waivers = tmp_path / "waivers.json"
        _setup_full_evidence(evidence, waivers)
        terminal = reconcile(str(evidence), str(waivers), "ci")
        assert terminal["state"] == TERMINAL_SUCCESS

    def test_terminal_file_created(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        waivers = tmp_path / "waivers.json"
        _setup_full_evidence(evidence, waivers)
        reconcile(str(evidence), str(waivers), "ci")
        assert (evidence / TERMINAL_FILE).exists()
        data = json.loads((evidence / TERMINAL_FILE).read_text())
        assert data["state"] == TERMINAL_SUCCESS


class TestDoubleFinalization:
    def test_second_reconcile_fails(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        waivers = tmp_path / "waivers.json"
        _setup_full_evidence(evidence, waivers)
        reconcile(str(evidence), str(waivers), "ci")
        with pytest.raises(EvidenceWriteError, match="already exists"):
            reconcile(str(evidence), str(waivers), "ci")


class TestIncomplete:
    def test_missing_manifest(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        waivers = tmp_path / "waivers.json"
        waivers.write_text(json.dumps({"schema_version": 1, "entries": []}) + "\n")
        terminal = reconcile(str(evidence), str(waivers), "ci")
        assert terminal["state"] == TERMINAL_INCOMPLETE


class TestTestFailed:
    def test_phase_failure(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        waivers = tmp_path / "waivers.json"
        _setup_full_evidence(evidence, waivers)
        (evidence / TERMINAL_FILE).unlink(missing_ok=True)
        (evidence / PHASE_RESULTS_FILE).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "lint",
                    "sequence": 1,
                    "exit_code": 1,
                    "failure_class": "nonzero_exit",
                    "start_utc": "2026-08-10T00:00:00+00:00",
                    "end_utc": "2026-08-10T00:00:01+00:00",
                }
            )
            + "\n"
        )
        terminal = reconcile(str(evidence), str(waivers), "ci")
        assert terminal["state"] == TERMINAL_TEST_FAILED


class TestEvidenceFailed:
    def test_collection_digest_mismatch(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        waivers = tmp_path / "waivers.json"
        _setup_full_evidence(evidence, waivers)
        (evidence / TERMINAL_FILE).unlink(missing_ok=True)
        manifest = json.loads((evidence / collection_manifest_file("non_pg")).read_text())
        manifest["nodes_digest"] = "wrong"
        (evidence / collection_manifest_file("non_pg")).write_text(json.dumps(manifest) + "\n")
        terminal = reconcile(str(evidence), str(waivers), "ci")
        assert terminal["state"] == TERMINAL_EVIDENCE_FAILED

    def test_pg_missing_call(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        waivers = tmp_path / "waivers.json"
        _setup_full_evidence(evidence, waivers)
        (evidence / TERMINAL_FILE).unlink(missing_ok=True)
        pg_node = "tests/integration/postgres/test_b.py::test_b"
        events_data = b""
        seq = 0
        for phase in ("setup", "teardown"):
            seq += 1
            event = {
                "schema_version": 1,
                "node_id": pg_node,
                "sequence": seq,
                "phase": phase,
                "outcome": "passed",
                "wasxfail": None,
            }
            events_data += (json.dumps(event, sort_keys=True) + "\n").encode()
        seq += 1
        final = {
            "schema_version": 1,
            "record_type": "final",
            "partition": "pg",
            "source_sha": "a" * 40,
            "environment": "ci",
            "final_sequence": seq,
            "selected_nodes_digest": digest_nodes([pg_node]),
            "preceding_stream_digest": digest_bytes(events_data),
            "pytest_exit_status": 0,
        }
        full = events_data + (json.dumps(final, sort_keys=True) + "\n").encode()
        (evidence / event_stream_file("pg")).write_bytes(full)
        terminal = reconcile(str(evidence), str(waivers), "ci")
        assert terminal["state"] in (TERMINAL_EVIDENCE_FAILED, TERMINAL_TEST_FAILED)
        assert any("call" in e.lower() for e in terminal["errors"])
