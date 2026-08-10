"""Tests for the pytest evidence plugin."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ci_evidence.schemas import (
    RUN_MANIFEST_FILE,
    collection_manifest_file,
    event_stream_file,
)
from ci_evidence.writer import TrustedRoot, exclusive_create_json


def _create_manifest(evidence: Path, sha: str = "a" * 40) -> None:
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
                "required_phases": [],
                "created_at_utc": "2026-08-10T00:00:00+00:00",
            },
        )


def _run_pytest(
    evidence: Path,
    test_file: Path,
    partition: str = "non_pg",
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "FONELY_EVIDENCE_ROOT": str(evidence),
        "FONELY_EVIDENCE_PARTITION": partition,
    }
    args = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "ci_evidence.pytest_plugin",
        str(test_file),
        "-q",
        "--tb=short",
        *(extra_args or []),
    ]
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent.parent),
    )


def _write_test(tmp_path: Path, name: str, content: str) -> Path:
    test_dir = tmp_path / "tests_synthetic"
    test_dir.mkdir(exist_ok=True)
    f = test_dir / name
    f.write_text(content)
    return f


class TestCollectionManifest:
    def test_creates_manifest(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        _create_manifest(evidence)
        content = "def test_a(): pass\ndef test_b(): pass\n"
        test_file = _write_test(tmp_path, "test_simple.py", content)
        r = _run_pytest(evidence, test_file)
        assert r.returncode == 0, r.stderr
        manifest = json.loads((evidence / collection_manifest_file("non_pg")).read_text())
        assert manifest["schema_version"] == 1
        assert manifest["node_count"] == 2
        assert len(manifest["nodes"]) == 2
        assert manifest["source_sha"] == "a" * 40

    def test_parametrized_literal_brackets(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        _create_manifest(evidence)
        test_file = _write_test(
            tmp_path,
            "test_param.py",
            """
import pytest
@pytest.mark.parametrize("x", [1, 2], ids=["[a]", "[b]"])
def test_p(x): pass
""",
        )
        r = _run_pytest(evidence, test_file)
        assert r.returncode == 0, r.stderr
        manifest = json.loads((evidence / collection_manifest_file("non_pg")).read_text())
        assert any("[" in n for n in manifest["nodes"])


class TestExecutionEvents:
    def test_emits_phase_events(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        _create_manifest(evidence)
        test_file = _write_test(tmp_path, "test_events.py", "def test_x(): pass\n")
        _run_pytest(evidence, test_file)
        stream = (evidence / event_stream_file("non_pg")).read_text().strip().splitlines()
        events = [json.loads(line) for line in stream]
        phase_events = [e for e in events if e.get("phase")]
        assert len(phase_events) == 3
        phases = [e["phase"] for e in phase_events]
        assert phases == ["setup", "call", "teardown"]

    def test_sequences_increment(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        _create_manifest(evidence)
        test_file = _write_test(tmp_path, "test_seq.py", "def test_a(): pass\ndef test_b(): pass\n")
        _run_pytest(evidence, test_file)
        stream = (evidence / event_stream_file("non_pg")).read_text().strip().splitlines()
        events = [json.loads(line) for line in stream]
        phase_events = [e for e in events if "sequence" in e]
        seqs = [e["sequence"] for e in phase_events]
        assert seqs == list(range(1, len(seqs) + 1))

    def test_failure_recorded(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        _create_manifest(evidence)
        test_file = _write_test(tmp_path, "test_fail.py", "def test_x(): assert False\n")
        r = _run_pytest(evidence, test_file)
        assert r.returncode != 0
        stream = (evidence / event_stream_file("non_pg")).read_text().strip().splitlines()
        events = [json.loads(line) for line in stream]
        call_events = [e for e in events if e.get("phase") == "call"]
        assert call_events[0]["outcome"] == "failed"


class TestFinalRecord:
    def test_final_record_present(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        _create_manifest(evidence)
        test_file = _write_test(tmp_path, "test_final.py", "def test_x(): pass\n")
        _run_pytest(evidence, test_file)
        stream = (evidence / event_stream_file("non_pg")).read_text().strip().splitlines()
        final = json.loads(stream[-1])
        assert final["record_type"] == "final"
        assert final["partition"] == "non_pg"
        assert final["source_sha"] == "a" * 40
        assert final["pytest_exit_status"] == 0

    def test_final_sequence_correct(self, tmp_path: Path) -> None:
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        _create_manifest(evidence)
        test_file = _write_test(tmp_path, "test_fseq.py", "def test_a(): pass\n")
        _run_pytest(evidence, test_file)
        stream = (evidence / event_stream_file("non_pg")).read_text().strip().splitlines()
        events = [json.loads(line) for line in stream]
        final = events[-1]
        assert final["final_sequence"] == len(events)


class TestNoEvidenceRoot:
    def test_skips_evidence_without_env(self, tmp_path: Path) -> None:
        test_file = _write_test(tmp_path, "test_no_ev.py", "def test_x(): pass\n")
        env = {k: v for k, v in os.environ.items() if k != "FONELY_EVIDENCE_ROOT"}
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "ci_evidence.pytest_plugin",
                str(test_file),
                "-q",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        assert r.returncode == 0
