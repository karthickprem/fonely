"""Tests for the dependency-free orchestrator."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ORCH = Path(__file__).resolve().parent.parent / "ci_evidence" / "orchestrator.py"


def run_orch(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ORCH), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=cwd,
    )


def init(tmp_path: Path) -> Path:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    r = run_orch(
        [
            "init",
            "--evidence-root",
            str(evidence),
            "--run-id",
            "12345",
            "--attempt",
            "1",
            "--environment",
            "ci",
        ]
    )
    assert r.returncode == 0, r.stderr
    return evidence


class TestInit:
    def test_creates_manifest(self, tmp_path: Path) -> None:
        evidence = init(tmp_path)
        manifest = json.loads((evidence / "run-manifest.json").read_text())
        assert manifest["schema_version"] == 1
        assert manifest["workflow_run_id"] == "12345"
        assert manifest["environment"] == "ci"
        assert len(manifest["source_sha"]) == 40
        assert len(manifest["source_tree"]) == 40
        assert len(manifest["required_phases"]) > 0

    def test_creates_empty_phase_results(self, tmp_path: Path) -> None:
        evidence = init(tmp_path)
        assert (evidence / "phase-results.jsonl").exists()
        assert (evidence / "phase-results.jsonl").stat().st_size == 0

    def test_double_init_fails(self, tmp_path: Path) -> None:
        evidence = init(tmp_path)
        r = run_orch(
            [
                "init",
                "--evidence-root",
                str(evidence),
                "--run-id",
                "99999",
                "--attempt",
                "1",
                "--environment",
                "ci",
            ]
        )
        assert r.returncode == 2

    def test_invalid_environment(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        r = run_orch(
            [
                "init",
                "--evidence-root",
                str(evidence),
                "--run-id",
                "1",
                "--attempt",
                "1",
                "--environment",
                "production",
            ]
        )
        assert r.returncode == 2


class TestPhase:
    def test_records_success(self, tmp_path: Path) -> None:
        evidence = init(tmp_path)
        r = run_orch(
            [
                "phase",
                "--evidence-root",
                str(evidence),
                "--phase",
                "lint",
                "--",
                "true",
            ]
        )
        assert r.returncode == 0
        lines = (evidence / "phase-results.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        result = json.loads(lines[0])
        assert result["phase"] == "lint"
        assert result["exit_code"] == 0
        assert result["failure_class"] is None
        assert result["sequence"] == 1

    def test_preserves_nonzero_exit(self, tmp_path: Path) -> None:
        evidence = init(tmp_path)
        r = run_orch(
            [
                "phase",
                "--evidence-root",
                str(evidence),
                "--phase",
                "lint",
                "--",
                "false",
            ]
        )
        assert r.returncode == 1
        result = json.loads((evidence / "phase-results.jsonl").read_text().strip().splitlines()[0])
        assert result["exit_code"] == 1
        assert result["failure_class"] == "nonzero_exit"

    def test_command_not_found(self, tmp_path: Path) -> None:
        evidence = init(tmp_path)
        r = run_orch(
            [
                "phase",
                "--evidence-root",
                str(evidence),
                "--phase",
                "lint",
                "--",
                "nonexistent_command_xyz",
            ]
        )
        assert r.returncode == 127

    def test_duplicate_phase_rejected(self, tmp_path: Path) -> None:
        evidence = init(tmp_path)
        run_orch(
            [
                "phase",
                "--evidence-root",
                str(evidence),
                "--phase",
                "lint",
                "--",
                "true",
            ]
        )
        r = run_orch(
            [
                "phase",
                "--evidence-root",
                str(evidence),
                "--phase",
                "lint",
                "--",
                "true",
            ]
        )
        assert r.returncode == 2
        assert "duplicate" in r.stderr.lower()

    def test_unknown_phase_rejected(self, tmp_path: Path) -> None:
        evidence = init(tmp_path)
        r = run_orch(
            [
                "phase",
                "--evidence-root",
                str(evidence),
                "--phase",
                "nonexistent_phase",
                "--",
                "true",
            ]
        )
        assert r.returncode == 2
        assert "unknown" in r.stderr.lower()

    def test_sequence_increments(self, tmp_path: Path) -> None:
        evidence = init(tmp_path)
        run_orch(
            [
                "phase",
                "--evidence-root",
                str(evidence),
                "--phase",
                "lint",
                "--",
                "true",
            ]
        )
        run_orch(
            [
                "phase",
                "--evidence-root",
                str(evidence),
                "--phase",
                "format_check",
                "--",
                "true",
            ]
        )
        lines = (evidence / "phase-results.jsonl").read_text().strip().splitlines()
        assert json.loads(lines[0])["sequence"] == 1
        assert json.loads(lines[1])["sequence"] == 2

    def test_missing_manifest(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        (evidence / "phase-results.jsonl").touch()
        r = run_orch(
            [
                "phase",
                "--evidence-root",
                str(evidence),
                "--phase",
                "lint",
                "--",
                "true",
            ]
        )
        assert r.returncode == 2

    def test_no_command_fails(self, tmp_path: Path) -> None:
        evidence = init(tmp_path)
        r = run_orch(
            [
                "phase",
                "--evidence-root",
                str(evidence),
                "--phase",
                "lint",
            ]
        )
        assert r.returncode == 2
