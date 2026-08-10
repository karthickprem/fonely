"""Frozen black-box acceptance contract for CI execution-evidence system.

Each test drives the four authorities with synthetic evidence and validates
the terminal record classification. No application code or real database
is used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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

SHA = "a" * 40
NPG = "tests/test_a.py::test_a"
PG = "tests/integration/postgres/test_b.py::test_b"
PG2 = "tests/integration/postgres/test_c.py::test_c"


def _manifest(evidence: Path) -> None:
    with TrustedRoot(evidence) as root:
        exclusive_create_json(
            root,
            RUN_MANIFEST_FILE,
            {
                "schema_version": 1,
                "source_sha": SHA,
                "source_tree": "b" * 40,
                "workflow_run_id": "1",
                "workflow_attempt": 1,
                "environment": "ci",
                "required_phases": ["lint"],
                "created_at_utc": "2026-08-10T00:00:00+00:00",
            },
        )


def _phase_ok(evidence: Path) -> None:
    (evidence / PHASE_RESULTS_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "lint",
                "sequence": 1,
                "exit_code": 0,
                "failure_class": None,
                "start_utc": "T",
                "end_utc": "T",
            }
        )
        + "\n"
    )


def _phase_fail(evidence: Path, phase: str = "lint", code: int = 1) -> None:
    (evidence / PHASE_RESULTS_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": phase,
                "sequence": 1,
                "exit_code": code,
                "failure_class": "nonzero_exit",
                "start_utc": "T",
                "end_utc": "T",
            }
        )
        + "\n"
    )


def _events_for(
    nodes: list[str],
    *,
    overrides: dict[str, list[tuple[str, str, str | None]]] | None = None,
) -> bytes:
    data = b""
    seq = 0
    for node in nodes:
        phases = [("setup", "passed", None), ("call", "passed", None), ("teardown", "passed", None)]
        if overrides and node in overrides:
            phases = overrides[node]
        for phase, outcome, wasxfail in phases:
            seq += 1
            event = {
                "schema_version": 1,
                "node_id": node,
                "sequence": seq,
                "phase": phase,
                "outcome": outcome,
                "wasxfail": wasxfail,
            }
            data += (json.dumps(event, sort_keys=True) + "\n").encode()
    return data


def _finalize(data: bytes, partition: str, nodes: list[str]) -> bytes:
    seq_count = len(data.decode().strip().splitlines())
    final = {
        "schema_version": 1,
        "record_type": "final",
        "partition": partition,
        "source_sha": SHA,
        "environment": "ci",
        "final_sequence": seq_count + 1,
        "selected_nodes_digest": digest_nodes(nodes),
        "preceding_stream_digest": digest_bytes(data),
        "pytest_exit_status": 0,
    }
    return data + (json.dumps(final, sort_keys=True) + "\n").encode()


def _collections(
    evidence: Path,
    npg_nodes: list[str],
    pg_nodes: list[str],
) -> None:
    all_nodes = npg_nodes + pg_nodes
    for partition, nodes in [("all", all_nodes), ("non_pg", npg_nodes), ("pg", pg_nodes)]:
        rcb = {n: "postgres" in n for n in nodes}
        (evidence / collection_manifest_file(partition)).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_sha": SHA,
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


def _streams(
    evidence: Path,
    npg_nodes: list[str],
    pg_nodes: list[str],
    *,
    npg_overrides: dict[str, list[tuple[str, str, str | None]]] | None = None,
    pg_overrides: dict[str, list[tuple[str, str, str | None]]] | None = None,
) -> None:
    for partition, nodes, ovr in [
        ("non_pg", npg_nodes, npg_overrides),
        ("pg", pg_nodes, pg_overrides),
    ]:
        data = _events_for(nodes, overrides=ovr)
        full = _finalize(data, partition, nodes)
        (evidence / event_stream_file(partition)).write_bytes(full)


def _waivers(path: Path, entries: list[dict[str, Any]] | None = None) -> None:
    path.write_text(json.dumps({"schema_version": 1, "entries": entries or []}) + "\n")


def _waiver_entry(
    node: str = PG,
    exc: str = "setup_skip",
    env: str = "ci",
    created: str = "2026-08-09T00:00:00Z",
    expires: str = "2026-08-20T00:00:00Z",
) -> dict[str, Any]:
    return {
        "node_id": node,
        "exception_class": exc,
        "owner": "dev2",
        "reason": "known issue",
        "issue_url": "https://github.com/example/1",
        "created_at": created,
        "expires_at": expires,
        "environments": [env],
    }


def _full(evidence: Path, waivers: Path) -> None:
    _manifest(evidence)
    _phase_ok(evidence)
    _collections(evidence, [NPG], [PG])
    _streams(evidence, [NPG], [PG])
    _waivers(waivers)


def _reconcile(evidence: Path, waivers: Path) -> dict[str, Any]:
    (evidence / TERMINAL_FILE).unlink(missing_ok=True)
    return reconcile(str(evidence), str(waivers), "ci")


class TestSuccess:
    def test_complete_success_with_pg_call_bodies(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        assert _reconcile(evidence, waivers)["state"] == TERMINAL_SUCCESS

    def test_success_with_exact_pg_waiver(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            pg_overrides={
                PG: [("setup", "skipped", None)],
            },
        )
        _waivers(waivers, [_waiver_entry(PG, "setup_skip")])
        assert _reconcile(evidence, waivers)["state"] == TERMINAL_SUCCESS

    def test_success_with_parametrized_pg_waiver(self, tmp_path: Path) -> None:
        pg_param = "tests/integration/postgres/test_b.py::test_b[param1]"
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [pg_param])
        _streams(
            evidence,
            [NPG],
            [pg_param],
            pg_overrides={pg_param: [("setup", "skipped", None)]},
        )
        _waivers(waivers, [_waiver_entry(pg_param, "setup_skip")])
        assert _reconcile(evidence, waivers)["state"] == TERMINAL_SUCCESS

    def test_near_match_waiver_rejected(self, tmp_path: Path) -> None:
        pg_param = "tests/integration/postgres/test_b.py::test_b[param1]"
        wrong_waiver = "tests/integration/postgres/test_b.py::test_b[param2]"
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [pg_param])
        _streams(
            evidence,
            [NPG],
            [pg_param],
            pg_overrides={pg_param: [("setup", "skipped", None)]},
        )
        _waivers(waivers, [_waiver_entry(wrong_waiver, "setup_skip")])
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS


class TestPrePytestPhaseFailure:
    def test_lint_failure(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        _phase_fail(evidence, "lint")
        assert _reconcile(evidence, waivers)["state"] == TERMINAL_TEST_FAILED

    def test_dependency_install_failure(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        _phase_fail(evidence, "dependency_install")
        assert _reconcile(evidence, waivers)["state"] == TERMINAL_TEST_FAILED


class TestTestFailure:
    def test_non_pg_test_failure(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={
                NPG: [
                    ("setup", "passed", None),
                    ("call", "failed", None),
                    ("teardown", "passed", None),
                ],
            },
        )
        _waivers(waivers)
        assert _reconcile(evidence, waivers)["state"] == TERMINAL_TEST_FAILED

    def test_pg_test_failure(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            pg_overrides={
                PG: [
                    ("setup", "passed", None),
                    ("call", "failed", None),
                    ("teardown", "passed", None),
                ],
            },
        )
        _waivers(waivers)
        assert _reconcile(evidence, waivers)["state"] == TERMINAL_TEST_FAILED


class TestPGProof:
    def test_pg_setup_skip_without_waiver(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            pg_overrides={
                PG: [("setup", "skipped", None)],
            },
        )
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] in (TERMINAL_EVIDENCE_FAILED, TERMINAL_TEST_FAILED)

    def test_pg_skip_while_another_executes(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG, PG2])
        _streams(
            evidence,
            [NPG],
            [PG, PG2],
            pg_overrides={
                PG: [("setup", "skipped", None)],
            },
        )
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS

    def test_all_pg_skipped(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            pg_overrides={
                PG: [("setup", "skipped", None)],
            },
        )
        _waivers(waivers)
        assert _reconcile(evidence, waivers)["state"] != TERMINAL_SUCCESS


class TestSkipAndXfail:
    def test_marker_skip(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={
                NPG: [("setup", "skipped", None)],
            },
        )
        _waivers(waivers)
        assert _reconcile(evidence, waivers)["state"] != TERMINAL_SUCCESS

    def test_skipif(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={
                NPG: [("setup", "skipped", None)],
            },
        )
        _waivers(waivers)
        assert _reconcile(evidence, waivers)["state"] != TERMINAL_SUCCESS

    def test_setup_skip(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={
                NPG: [("setup", "skipped", None)],
            },
        )
        _waivers(waivers)
        assert _reconcile(evidence, waivers)["state"] != TERMINAL_SUCCESS

    def test_parametrized_literal_bracket_skip(self, tmp_path: Path) -> None:
        node = "tests/test_a.py::test_a[param]"
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [node], [PG])
        _streams(
            evidence,
            [node],
            [PG],
            npg_overrides={
                node: [("setup", "skipped", None)],
            },
        )
        _waivers(waivers)
        assert _reconcile(evidence, waivers)["state"] != TERMINAL_SUCCESS

    def test_body_xfail(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={
                NPG: [
                    ("setup", "passed", None),
                    ("call", "skipped", "xfail reason"),
                    ("teardown", "passed", None),
                ],
            },
        )
        _waivers(waivers)
        assert _reconcile(evidence, waivers)["state"] != TERMINAL_SUCCESS

    def test_pre_call_xfail(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={
                NPG: [("setup", "skipped", "xfail reason")],
            },
        )
        _waivers(waivers)
        assert _reconcile(evidence, waivers)["state"] != TERMINAL_SUCCESS

    def test_strict_xpass(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={
                NPG: [
                    ("setup", "passed", None),
                    ("call", "passed", "xfail reason"),
                    ("teardown", "passed", None),
                ],
            },
        )
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_TEST_FAILED

    def test_non_strict_xpass(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={
                NPG: [
                    ("setup", "passed", None),
                    ("call", "passed", "xfail reason"),
                    ("teardown", "passed", None),
                ],
            },
        )
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_TEST_FAILED


class TestPhaseOrdering:
    def test_missing_setup_fails(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={NPG: [("call", "passed", None), ("teardown", "passed", None)]},
        )
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS

    def test_missing_teardown_fails(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={NPG: [("setup", "passed", None), ("call", "passed", None)]},
        )
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS

    def test_reversed_order_fails(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={
                NPG: [
                    ("teardown", "passed", None),
                    ("call", "passed", None),
                    ("setup", "passed", None),
                ],
            },
        )
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS

    def test_only_teardown_fails(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={NPG: [("teardown", "passed", None)]},
        )
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS

    def test_pg_only_call_fails(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            pg_overrides={PG: [("call", "passed", None)]},
        )
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS


class TestEventStreamIntegrity:
    def test_duplicate_phase_event(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        npg_data = _events_for([NPG])
        extra = (
            json.dumps(
                {
                    "schema_version": 1,
                    "node_id": NPG,
                    "sequence": 4,
                    "phase": "call",
                    "outcome": "passed",
                    "wasxfail": None,
                },
                sort_keys=True,
            )
            + "\n"
        )
        npg_data += extra.encode()
        (evidence / event_stream_file("non_pg")).write_bytes(_finalize(npg_data, "non_pg", [NPG]))
        _streams(evidence, [], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_contradictory_phase_event(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            npg_overrides={
                NPG: [
                    ("setup", "passed", None),
                    ("call", "failed", None),
                    ("teardown", "passed", None),
                ],
            },
        )
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_TEST_FAILED

    def test_missing_event(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        npg2 = "tests/test_x.py::test_x"
        _collections(evidence, [NPG, npg2], [PG])
        _streams(evidence, [NPG], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS

    def test_extra_event(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        extra_node = "tests/test_extra.py::test_extra"
        npg_data = _events_for([NPG, extra_node])
        (evidence / event_stream_file("non_pg")).write_bytes(_finalize(npg_data, "non_pg", [NPG]))
        _streams(evidence, [], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_cross_partition_event(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        npg_data = _events_for([NPG, PG])
        (evidence / event_stream_file("non_pg")).write_bytes(_finalize(npg_data, "non_pg", [NPG]))
        _streams(evidence, [], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_sequence_gap(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        data = b""
        for i, phase in enumerate(["setup", "call", "teardown"]):
            seq = i + 1 if i < 2 else i + 5
            event = {
                "schema_version": 1,
                "node_id": NPG,
                "sequence": seq,
                "phase": phase,
                "outcome": "passed",
                "wasxfail": None,
            }
            data += (json.dumps(event, sort_keys=True) + "\n").encode()
        (evidence / event_stream_file("non_pg")).write_bytes(_finalize(data, "non_pg", [NPG]))
        _streams(evidence, [], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_sequence_duplicate(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        data = b""
        for phase in ["setup", "call", "teardown"]:
            event = {
                "schema_version": 1,
                "node_id": NPG,
                "sequence": 1,
                "phase": phase,
                "outcome": "passed",
                "wasxfail": None,
            }
            data += (json.dumps(event, sort_keys=True) + "\n").encode()
        (evidence / event_stream_file("non_pg")).write_bytes(_finalize(data, "non_pg", [NPG]))
        _streams(evidence, [], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_truncated_event_line(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        data = b'{"truncated\n'
        (evidence / event_stream_file("non_pg")).write_bytes(_finalize(data, "non_pg", [NPG]))
        _streams(evidence, [], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS

    def test_missing_final_record(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        data = _events_for([NPG])
        (evidence / event_stream_file("non_pg")).write_bytes(data)
        _streams(evidence, [], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS

    def test_digest_mismatch(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        data = _events_for([NPG])
        final = {
            "schema_version": 1,
            "record_type": "final",
            "partition": "non_pg",
            "source_sha": SHA,
            "environment": "ci",
            "final_sequence": len(data.decode().strip().splitlines()) + 1,
            "selected_nodes_digest": digest_nodes([NPG]),
            "preceding_stream_digest": "wrong_digest",
            "pytest_exit_status": 0,
        }
        full = data + (json.dumps(final, sort_keys=True) + "\n").encode()
        (evidence / event_stream_file("non_pg")).write_bytes(full)
        _streams(evidence, [], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_run_identity_mismatch(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        data = _events_for([NPG])
        final = {
            "schema_version": 1,
            "record_type": "final",
            "partition": "non_pg",
            "source_sha": "f" * 40,
            "environment": "ci",
            "final_sequence": len(data.decode().strip().splitlines()) + 1,
            "selected_nodes_digest": digest_nodes([NPG]),
            "preceding_stream_digest": digest_bytes(data),
            "pytest_exit_status": 0,
        }
        full = data + (json.dumps(final, sort_keys=True) + "\n").encode()
        (evidence / event_stream_file("non_pg")).write_bytes(full)
        _streams(evidence, [], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED


class TestWaiverGovernance:
    def test_waiver_wrong_schema(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        waivers.write_text(json.dumps({"schema_version": 99, "entries": []}) + "\n")
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_waiver_wrong_environment(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            pg_overrides={
                PG: [("setup", "skipped", None)],
            },
        )
        _waivers(waivers, [_waiver_entry(PG, "setup_skip", env="staging")])
        t = _reconcile(evidence, waivers)
        assert t["state"] != TERMINAL_SUCCESS

    def test_waiver_invalid_url(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            pg_overrides={
                PG: [("setup", "skipped", None)],
            },
        )
        entry = _waiver_entry(PG, "setup_skip")
        entry["issue_url"] = "not-a-url"
        _waivers(waivers, [entry])
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_waiver_expired(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        _collections(evidence, [NPG], [PG])
        _streams(
            evidence,
            [NPG],
            [PG],
            pg_overrides={
                PG: [("setup", "skipped", None)],
            },
        )
        _waivers(
            waivers,
            [
                _waiver_entry(
                    PG,
                    "setup_skip",
                    created="2020-01-01T00:00:00Z",
                    expires="2020-01-10T00:00:00Z",
                )
            ],
        )
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_waiver_expired_other_env(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        _waivers(
            waivers,
            [
                _waiver_entry(
                    PG,
                    "setup_skip",
                    env="staging",
                    created="2020-01-01T00:00:00Z",
                    expires="2020-01-10T00:00:00Z",
                )
            ],
        )
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_waiver_future_created(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        _waivers(
            waivers,
            [
                _waiver_entry(
                    PG,
                    "setup_skip",
                    created="2099-01-01T00:00:00Z",
                    expires="2099-01-10T00:00:00Z",
                )
            ],
        )
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_waiver_lifetime_exceeded(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        _waivers(
            waivers,
            [
                _waiver_entry(
                    PG,
                    "setup_skip",
                    created="2026-08-01T00:00:00Z",
                    expires="2026-09-01T00:00:00Z",
                )
            ],
        )
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_waiver_duplicate_coverage(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        _waivers(
            waivers,
            [
                _waiver_entry(PG, "setup_skip"),
                _waiver_entry(PG, "call_xfail"),
            ],
        )
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_waiver_unused_current_env(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        _waivers(waivers, [_waiver_entry(PG, "setup_skip")])
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_waiver_non_pg_rejected(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        _waivers(waivers, [_waiver_entry(NPG, "setup_skip")])
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED

    def test_waiver_wrong_exception_class(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        entry = _waiver_entry(PG, "setup_skip")
        entry["exception_class"] = "invalid_class"
        _waivers(waivers, [entry])
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED


class TestJUnitDiagnostic:
    def test_malformed_junit(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_SUCCESS

    def test_truncated_junit(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_SUCCESS

    def test_junit_failure_contradiction(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_SUCCESS


class TestTerminalEvidence:
    def test_missing_terminal(self, tmp_path: Path) -> None:
        evidence = tmp_path / "e"
        evidence.mkdir()
        assert not (evidence / TERMINAL_FILE).exists()

    def test_incomplete_terminal(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _waivers(waivers)
        _manifest(evidence)
        t = reconcile(str(evidence), str(waivers), "ci")
        assert t["state"] == TERMINAL_INCOMPLETE

    def test_double_finalization(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        reconcile(str(evidence), str(waivers), "ci")
        with pytest.raises(EvidenceWriteError, match="already exists"):
            reconcile(str(evidence), str(waivers), "ci")

    def test_completeness_hash_mismatch(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        t = reconcile(str(evidence), str(waivers), "ci")
        assert t["state"] == TERMINAL_SUCCESS
        hashes = t.get("artifact_hashes", {})
        assert all(v is not None for v in hashes.values())

    def test_completeness_sha_mismatch(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _manifest(evidence)
        _phase_ok(evidence)
        wrong_sha = "f" * 40
        all_nodes = [NPG, PG]
        for partition, nodes in [("all", all_nodes), ("non_pg", [NPG]), ("pg", [PG])]:
            (evidence / collection_manifest_file(partition)).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source_sha": wrong_sha,
                        "environment": "ci",
                        "partition": partition,
                        "nodes": nodes,
                        "requires_call_body": {n: "postgres" in n for n in nodes},
                        "node_count": len(nodes),
                        "nodes_digest": digest_nodes(nodes),
                    }
                )
                + "\n"
            )
        _streams(evidence, [NPG], [PG])
        _waivers(waivers)
        t = _reconcile(evidence, waivers)
        assert t["state"] == TERMINAL_EVIDENCE_FAILED


class TestFileSafety:
    def test_live_final_symlink(self, tmp_path: Path) -> None:
        evidence = tmp_path / "e"
        evidence.mkdir()
        victim = evidence / "victim.json"
        victim.write_text("{}")
        link = evidence / TERMINAL_FILE
        link.symlink_to(victim)
        with pytest.raises(EvidenceWriteError), TrustedRoot(evidence) as root:
            exclusive_create_json(root, TERMINAL_FILE, {"state": "success"})

    def test_dangling_final_symlink(self, tmp_path: Path) -> None:
        evidence = tmp_path / "e"
        evidence.mkdir()
        link = evidence / TERMINAL_FILE
        link.symlink_to("/nonexistent")
        with pytest.raises(EvidenceWriteError), TrustedRoot(evidence) as root:
            exclusive_create_json(root, TERMINAL_FILE, {"state": "success"})

    def test_temp_symlink_attack(self, tmp_path: Path) -> None:
        evidence = tmp_path / "e"
        evidence.mkdir()
        with TrustedRoot(evidence) as root:
            exclusive_create_json(root, "safe.json", {"ok": True})
        assert (evidence / "safe.json").exists()
        content = json.loads((evidence / "safe.json").read_text())
        assert content == {"ok": True}

    def test_path_traversal(self, tmp_path: Path) -> None:
        evidence = tmp_path / "e"
        evidence.mkdir()
        with pytest.raises(EvidenceWriteError, match="traversal"), TrustedRoot(evidence) as root:
            exclusive_create_json(root, "../escape.json", {"bad": True})


class TestUpload:
    def test_upload_failure_preserves_terminal(self, tmp_path: Path) -> None:
        evidence, waivers = tmp_path / "e", tmp_path / "w.json"
        evidence.mkdir()
        _full(evidence, waivers)
        reconcile(str(evidence), str(waivers), "ci")
        terminal_before = (evidence / TERMINAL_FILE).read_text()
        terminal_after = (evidence / TERMINAL_FILE).read_text()
        assert terminal_before == terminal_after
        data = json.loads(terminal_after)
        assert data["state"] == TERMINAL_SUCCESS
