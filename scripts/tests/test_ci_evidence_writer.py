"""Adversarial tests for the shared secure evidence writer."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from ci_evidence.writer import (
    EvidenceWriteError,
    TrustedRoot,
    append_jsonl,
    atomic_replace_json,
    exclusive_create_json,
    safe_read,
)


@pytest.fixture()
def root(tmp_path: Path) -> TrustedRoot:
    r = TrustedRoot(tmp_path)
    yield r
    r.close()


DATA = {"key": "value", "schema_version": 1}


class TestTrustedRoot:
    def test_valid_directory(self, tmp_path: Path) -> None:
        with TrustedRoot(tmp_path) as r:
            assert r.path == tmp_path.resolve()

    def test_relative_path_rejected(self) -> None:
        with pytest.raises(EvidenceWriteError, match="absolute"):
            TrustedRoot("relative/path")

    def test_nonexistent_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(EvidenceWriteError, match="cannot resolve"):
            TrustedRoot(tmp_path / "nonexistent")

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        with pytest.raises(EvidenceWriteError, match="real directory"):
            TrustedRoot(link)

    def test_path_traversal_rejected(self, root: TrustedRoot) -> None:
        with pytest.raises(EvidenceWriteError, match="traversal"):
            root.resolve("../outside")


class TestExclusiveCreateJson:
    def test_creates_file(self, root: TrustedRoot) -> None:
        p = exclusive_create_json(root, "test.json", DATA)
        assert json.loads(p.read_text()) == DATA

    def test_permissions(self, root: TrustedRoot) -> None:
        p = exclusive_create_json(root, "test.json", DATA)
        assert stat.S_IMODE(os.lstat(p).st_mode) == 0o600

    def test_double_create_fails(self, root: TrustedRoot) -> None:
        exclusive_create_json(root, "test.json", DATA)
        with pytest.raises(EvidenceWriteError, match="already exists"):
            exclusive_create_json(root, "test.json", DATA)

    def test_creates_parent_dirs(self, root: TrustedRoot) -> None:
        p = exclusive_create_json(root, "sub/dir/test.json", DATA)
        assert p.exists()

    def test_symlink_target_rejected(self, root: TrustedRoot) -> None:
        victim = root.path / "victim.json"
        victim.write_text("{}")
        link = root.path / "link.json"
        link.symlink_to(victim)
        with pytest.raises(EvidenceWriteError, match="symlink"):
            exclusive_create_json(root, "link.json", DATA)

    def test_dangling_symlink_rejected(self, root: TrustedRoot) -> None:
        link = root.path / "dangling.json"
        link.symlink_to("/nonexistent/path")
        with pytest.raises(EvidenceWriteError):
            exclusive_create_json(root, "dangling.json", DATA)

    def test_traversal_rejected(self, root: TrustedRoot) -> None:
        with pytest.raises(EvidenceWriteError, match="traversal"):
            exclusive_create_json(root, "../escape.json", DATA)


class TestAtomicReplaceJson:
    def test_creates_new_file(self, root: TrustedRoot) -> None:
        p = atomic_replace_json(root, "test.json", DATA)
        assert json.loads(p.read_text()) == DATA

    def test_replaces_existing(self, root: TrustedRoot) -> None:
        atomic_replace_json(root, "test.json", {"old": True})
        p = atomic_replace_json(root, "test.json", DATA)
        assert json.loads(p.read_text()) == DATA

    def test_permissions(self, root: TrustedRoot) -> None:
        p = atomic_replace_json(root, "test.json", DATA)
        assert stat.S_IMODE(os.lstat(p).st_mode) == 0o600

    def test_symlink_target_rejected(self, root: TrustedRoot) -> None:
        victim = root.path / "victim.json"
        victim.write_text("{}")
        link = root.path / "link.json"
        link.symlink_to(victim)
        with pytest.raises(EvidenceWriteError, match="symlink"):
            atomic_replace_json(root, "link.json", DATA)

    def test_no_temp_left_on_success(self, root: TrustedRoot) -> None:
        atomic_replace_json(root, "test.json", DATA)
        temps = [f for f in os.listdir(root.path) if f.startswith(".test.json.")]
        assert temps == []


class TestAppendJsonl:
    def test_appends_record(self, root: TrustedRoot) -> None:
        append_jsonl(root, "events.jsonl", {"seq": 1})
        append_jsonl(root, "events.jsonl", {"seq": 2})
        lines = (root.path / "events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["seq"] == 1
        assert json.loads(lines[1])["seq"] == 2

    def test_each_line_valid_json(self, root: TrustedRoot) -> None:
        for i in range(5):
            append_jsonl(root, "e.jsonl", {"i": i})
        for line in (root.path / "e.jsonl").read_text().strip().splitlines():
            json.loads(line)

    def test_symlink_rejected(self, root: TrustedRoot) -> None:
        victim = root.path / "victim.jsonl"
        victim.write_text("")
        link = root.path / "link.jsonl"
        link.symlink_to(victim)
        with pytest.raises(EvidenceWriteError, match="symlink"):
            append_jsonl(root, "link.jsonl", {"x": 1})


class TestSafeRead:
    def test_reads_file(self, root: TrustedRoot) -> None:
        (root.path / "data.json").write_bytes(b'{"ok": true}')
        assert json.loads(safe_read(root, "data.json")) == {"ok": True}

    def test_missing_file(self, root: TrustedRoot) -> None:
        with pytest.raises(EvidenceWriteError, match="not found"):
            safe_read(root, "missing.json")

    def test_empty_file(self, root: TrustedRoot) -> None:
        (root.path / "empty.json").write_bytes(b"")
        with pytest.raises(EvidenceWriteError, match="invalid file size"):
            safe_read(root, "empty.json")

    def test_symlink_rejected(self, root: TrustedRoot) -> None:
        real = root.path / "real.json"
        real.write_bytes(b'{"x": 1}')
        link = root.path / "link.json"
        link.symlink_to(real)
        with pytest.raises(EvidenceWriteError, match="symlink"):
            safe_read(root, "link.json")

    def test_traversal_rejected(self, root: TrustedRoot) -> None:
        with pytest.raises(EvidenceWriteError, match="traversal"):
            safe_read(root, "../etc/passwd")
