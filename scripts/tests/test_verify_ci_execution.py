"""Tests for verify-test-partitions.py and verify-test-execution.py.

Covers partitions, execution, skips, xfail/xpass, adversarial inputs,
security bounds, error collection, and allowed-skips schema enforcement.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PARTITIONS = SCRIPTS_DIR / "verify-test-partitions.py"
EXECUTION = SCRIPTS_DIR / "verify-test-execution.py"


def _run(script: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _w(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content)
    return p


def _collect(*nodes: str, collected: int | None = None, deselected: int = 0) -> str:
    lines = list(nodes)
    lines.append("")
    c = collected or len(nodes)
    if deselected:
        lines.append(f"{c}/{c + deselected} tests collected ({deselected} deselected) in 1.0s")
    else:
        lines.append(f"{c} tests collected in 1.0s")
    return "\n".join(lines)


def _junit(*tcs: str) -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name="pytest" tests="{len(tcs)}">\n' + "".join(tcs) + "</testsuite>\n"
    )


def _tc(cls: str, name: str, outcome: str = "passed") -> str:
    tag = f'<testcase classname="{cls}" name="{name}" time="0.01"'
    t0 = f'<testcase classname="{cls}" name="{name}" time="0.00"'
    if outcome == "passed":
        return f"  {tag}/>\n"
    if outcome == "failed":
        return f'  {tag}><failure message="fail"/></testcase>\n'
    if outcome == "error":
        return f'  {tag}><error message="err"/></testcase>\n'
    if outcome == "skipped":
        return f'  {t0}><skipped message="skip"/></testcase>\n'
    if outcome == "xfail":
        return f'  {tag}><skipped message="xfail: expected"/></testcase>\n'
    if outcome == "xpass":
        return f'  {tag}><skipped message="xpass: strict"/></testcase>\n'
    return f"  {tag}/>\n"


def _inv(tmp: Path, all_c: int = 3, pg: int = 1) -> Path:
    d = {
        "schema_version": 1,
        "valid": True,
        "errors": [],
        "counts": {"all": all_c, "non_pg": all_c - pg, "pg": pg},
    }
    return _w(tmp, "inv.json", json.dumps(d))


def _al(tmp: Path, entries: list[dict] | None = None) -> Path:
    return _w(tmp, "skips.json", json.dumps({"schema_version": 1, "entries": entries or []}))


# ============================================================================
# PARTITION TESTS (17 cases)
# ============================================================================


class TestPartitionValid:
    def test_exact_disjoint(self, tmp_path: Path) -> None:
        a = _w(tmp_path, "a.txt", _collect("tests/t.py::t1", "tests/pg/t.py::t2"))
        n = _w(tmp_path, "n.txt", _collect("tests/t.py::t1", deselected=1))
        p = _w(tmp_path, "p.txt", _collect("tests/pg/t.py::t2", deselected=1))
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r)],
            ).returncode
            == 0
        )
        assert json.loads(r.read_text())["valid"] is True

    def test_parametrized_nodes(self, tmp_path: Path) -> None:
        nodes = ["tests/t.py::T::t[a-1]", "tests/t.py::T::t[b 2]", "tests/pg/t.py::t[x]"]
        a = _w(tmp_path, "a.txt", _collect(*nodes))
        n = _w(tmp_path, "n.txt", _collect(*nodes[:2], deselected=1))
        p = _w(tmp_path, "p.txt", _collect(nodes[2], deselected=2))
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r)],
            ).returncode
            == 0
        )


class TestPartitionOverlap:
    def test_overlap(self, tmp_path: Path) -> None:
        n = "tests/t.py::t1"
        a = _w(tmp_path, "a.txt", _collect(n))
        r = tmp_path / "r.json"
        res = _run(
            PARTITIONS,
            [
                "--all",
                str(a),
                "--non-pg",
                str(_w(tmp_path, "n.txt", _collect(n))),
                "--pg",
                str(_w(tmp_path, "p.txt", _collect(n))),
                "--report",
                str(r),
            ],
        )
        assert res.returncode == 1
        assert "overlap" in res.stderr.lower()


class TestPartitionOmission:
    def test_missing_from_partitions(self, tmp_path: Path) -> None:
        a = _w(tmp_path, "a.txt", _collect("tests/t.py::t1", "tests/t.py::t2"))
        n = _w(tmp_path, "n.txt", _collect("tests/t.py::t1", deselected=1))
        p = _w(tmp_path, "p.txt", _collect(deselected=2))
        r = tmp_path / "r.json"
        res = _run(
            PARTITIONS, ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r)]
        )
        assert res.returncode == 1


class TestPartitionEmpty:
    def test_empty_pg(self, tmp_path: Path) -> None:
        a = _w(tmp_path, "a.txt", _collect("tests/t.py::t1"))
        n = _w(tmp_path, "n.txt", _collect("tests/t.py::t1"))
        p = _w(tmp_path, "p.txt", _collect(deselected=1))
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r)],
            ).returncode
            == 1
        )


class TestPartitionDuplicate:
    def test_dup_in_all(self, tmp_path: Path) -> None:
        a = _w(tmp_path, "a.txt", _collect("tests/t.py::t1", "tests/t.py::t1"))
        n = _w(tmp_path, "n.txt", _collect("tests/t.py::t1"))
        p = _w(tmp_path, "p.txt", _collect("tests/pg/t.py::t2", deselected=1))
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r)],
            ).returncode
            == 1
        )


class TestPartitionFooter:
    def test_warnings_ignored(self, tmp_path: Path) -> None:
        txt = "tests/t.py::t1\n\n  DeprecationWarning: x\n\n-- Docs\n1 tests collected in 0.5s\n"
        a = _w(tmp_path, "a.txt", txt)
        pg = "tests/pg/t.py::t2\n\n1/2 tests collected (1 deselected) in 0.5s\n"
        all_txt = "tests/t.py::t1\ntests/pg/t.py::t2\n\n2 tests collected in 0.5s\n"
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                [
                    "--all",
                    str(_w(tmp_path, "a2.txt", all_txt)),
                    "--non-pg",
                    str(a),
                    "--pg",
                    str(_w(tmp_path, "p.txt", pg)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 0
        )


class TestPartitionError:
    def test_collection_error_fails(self, tmp_path: Path) -> None:
        err = (
            "tests/t.py::t1\n\n====== ERRORS ======\n"
            "ERROR collecting tests/broken.py\n\n"
            "0 tests collected in 0.5s\n"
        )
        a = _w(tmp_path, "a.txt", err)
        n = _w(tmp_path, "n.txt", _collect("tests/t.py::t1"))
        p = _w(tmp_path, "p.txt", _collect("tests/pg/t.py::t2", deselected=1))
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r)],
            ).returncode
            == 1
        )
        assert (
            "errors"
            in _run(
                PARTITIONS,
                ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r)],
            ).stderr.lower()
        )

    def test_no_footer_fails(self, tmp_path: Path) -> None:
        a = _w(tmp_path, "a.txt", "tests/t.py::t1\n")
        n = _w(tmp_path, "n.txt", "tests/t.py::t1\n")
        p = _w(tmp_path, "p.txt", "tests/pg/t.py::t2\n")
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r)],
            ).returncode
            == 1
        )

    def test_truncated_output_fails(self, tmp_path: Path) -> None:
        a = _w(tmp_path, "a.txt", "tests/t.py::t1\ntests/t.py::t2\n")
        n = _w(tmp_path, "n.txt", _collect("tests/t.py::t1"))
        p = _w(tmp_path, "p.txt", _collect("tests/pg/t.py::t2", deselected=1))
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r)],
            ).returncode
            == 1
        )


class TestPartitionMissing:
    def test_missing_file(self, tmp_path: Path) -> None:
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                [
                    "--all",
                    str(tmp_path / "x"),
                    "--non-pg",
                    str(tmp_path / "y"),
                    "--pg",
                    str(tmp_path / "z"),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 2
        )


class TestPartitionSymlink:
    def test_symlink_rejected(self, tmp_path: Path) -> None:
        real = _w(tmp_path, "real.txt", _collect("tests/t.py::t1"))
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                ["--all", str(link), "--non-pg", str(real), "--pg", str(real), "--report", str(r)],
            ).returncode
            == 2
        )


class TestPartitionDigest:
    def test_deterministic(self, tmp_path: Path) -> None:
        a = _w(tmp_path, "a.txt", _collect("tests/t.py::t1", "tests/pg/t.py::t2"))
        n = _w(tmp_path, "n.txt", _collect("tests/t.py::t1", deselected=1))
        p = _w(tmp_path, "p.txt", _collect("tests/pg/t.py::t2", deselected=1))
        r1 = tmp_path / "r1.json"
        r2 = tmp_path / "r2.json"
        _run(PARTITIONS, ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r1)])
        _run(PARTITIONS, ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r2)])
        assert json.loads(r1.read_text())["digests"] == json.loads(r2.read_text())["digests"]


class TestPartitionFooterCount:
    def test_footer_mismatch(self, tmp_path: Path) -> None:
        a = _w(tmp_path, "a.txt", "tests/t.py::t1\n\n5 tests collected in 1.0s\n")
        n = _w(tmp_path, "n.txt", _collect("tests/t.py::t1"))
        p = _w(tmp_path, "p.txt", _collect("tests/pg/t.py::t2", deselected=1))
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                ["--all", str(a), "--non-pg", str(n), "--pg", str(p), "--report", str(r)],
            ).returncode
            == 1
        )


# ============================================================================
# EXECUTION TESTS (26+ cases)
# ============================================================================


class TestExecAllPassed:
    def test_all_pass(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 2, 1)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1"), _tc("tests.integration.postgres.test_b", "t2")),
        )
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 0
        )
        assert json.loads(r.read_text())["counts"]["passed"] == 2


class TestExecUnexpectedSkip:
    def test_fails(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 2, 1)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"), _tc("tests.integration.postgres.test_b", "t2", "skipped")
            ),
        )
        r = tmp_path / "r.json"
        res = _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j),
                "--skip-allowlist",
                str(_al(tmp_path)),
                "--report",
                str(r),
            ],
        )
        assert res.returncode == 1
        assert "unexpected skip" in res.stderr.lower()


class TestExecAllowedSkip:
    def test_passes(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 3, 2)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc("tests.integration.postgres.test_b", "t2", "skipped"),
                _tc("tests.integration.postgres.test_c", "t3"),
            ),
        )
        al = _al(
            tmp_path,
            [
                {
                    "node_id_pattern": "tests/integration/postgres/test_b.py::t2",
                    "owner": "dev2",
                    "issue_url": "https://x/1",
                    "reason": "known flaky",
                    "created_at": "2026-08-09T00:00:00Z",
                    "expires_at": "2026-08-23T00:00:00Z",
                    "environments": ["ci"],
                }
            ],
        )
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(al),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 0
        )
        assert json.loads(r.read_text())["counts"]["allowed_skip"] == 1


class TestExecExpiredAllowlist:
    def test_fails(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "t1")))
        al = _al(
            tmp_path,
            [
                {
                    "node_id_pattern": "tests/old.py::t",
                    "owner": "x",
                    "issue_url": "https://x/2",
                    "reason": "old",
                    "created_at": "2026-07-01T00:00:00Z",
                    "expires_at": "2026-07-15T00:00:00Z",
                    "environments": ["ci"],
                }
            ],
        )
        r = tmp_path / "r.json"
        res = _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j),
                "--skip-allowlist",
                str(al),
                "--report",
                str(r),
            ],
        )
        assert res.returncode == 1
        assert "expired" in res.stderr.lower()


class TestExecZeroPg:
    def test_fails(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 2, 1)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc("tests.integration.postgres.test_b", "t2", "skipped"),
            ),
        )
        al = _al(
            tmp_path,
            [
                {
                    "node_id_pattern": "tests/integration/postgres/test_b.py::t2",
                    "owner": "x",
                    "issue_url": "https://x/3",
                    "reason": "temp",
                    "created_at": "2026-08-09T00:00:00Z",
                    "expires_at": "2026-08-23T00:00:00Z",
                    "environments": ["ci"],
                }
            ],
        )
        r = tmp_path / "r.json"
        res = _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j),
                "--skip-allowlist",
                str(al),
                "--report",
                str(r),
            ],
        )
        assert res.returncode == 1
        assert "zero executed" in res.stderr.lower()


class TestExecXfail:
    def test_counted(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "t1", "xfail")))
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 0
        )
        assert json.loads(r.read_text())["counts"]["xfail"] == 1


class TestExecXpass:
    def test_strict_xpass_fails(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "t1", "xpass")))
        r = tmp_path / "r.json"
        res = _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j),
                "--skip-allowlist",
                str(_al(tmp_path)),
                "--report",
                str(r),
            ],
        )
        assert res.returncode == 1
        assert "xpass" in res.stderr.lower()


class TestExecFailure:
    def test_counted_not_blocked(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 2, 0)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1"), _tc("tests.test_b", "t2", "failed")),
        )
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 0
        )
        assert json.loads(r.read_text())["counts"]["failed"] == 1


class TestExecError:
    def test_counted(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "t1", "error")))
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 0
        )


class TestExecMultiJunit:
    def test_two_files(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 2, 1)
        j1 = _w(tmp_path, "n.xml", _junit(_tc("tests.test_a", "t1")))
        j2 = _w(tmp_path, "p.xml", _junit(_tc("tests.integration.postgres.test_b", "t2")))
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j1),
                    "--junit",
                    str(j2),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 0
        )


class TestExecMalformedXml:
    def test_exits_2(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "bad.xml", "not xml <broken")
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 2
        )
        assert (
            "malformed"
            in _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).stderr.lower()
        )


class TestExecEmptyJunit:
    def test_fails(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "e.xml", _junit())
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 1
        )


class TestExecInvalidInventory:
    def test_fails(self, tmp_path: Path) -> None:
        inv = _w(tmp_path, "inv.json", '{"valid": false, "errors": ["x"]}')
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "t1")))
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 1
        )


class TestExecMissingInventory:
    def test_exits_2(self, tmp_path: Path) -> None:
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "t1")))
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(tmp_path / "x.json"),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 2
        )


class TestExecDigest:
    def test_deterministic(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "t1")))
        al = _al(tmp_path)
        r1 = tmp_path / "r1.json"
        r2 = tmp_path / "r2.json"
        _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j),
                "--skip-allowlist",
                str(al),
                "--report",
                str(r1),
            ],
        )
        _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j),
                "--skip-allowlist",
                str(al),
                "--report",
                str(r2),
            ],
        )
        assert json.loads(r1.read_text())["digest"] == json.loads(r2.read_text())["digest"]


class TestExecNodeDigest:
    def test_includes_outcomes(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 2, 0)
        j1 = _w(tmp_path, "j1.xml", _junit(_tc("tests.test_a", "t1"), _tc("tests.test_b", "t2")))
        j2 = _w(
            tmp_path,
            "j2.xml",
            _junit(_tc("tests.test_a", "t1"), _tc("tests.test_b", "t2", "failed")),
        )
        al = _al(tmp_path)
        r1 = tmp_path / "r1.json"
        r2 = tmp_path / "r2.json"
        _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j1),
                "--skip-allowlist",
                str(al),
                "--report",
                str(r1),
            ],
        )
        _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j2),
                "--skip-allowlist",
                str(al),
                "--report",
                str(r2),
            ],
        )
        assert json.loads(r1.read_text())["digest"] != json.loads(r2.read_text())["digest"]


class TestExecSymlink:
    def test_rejected(self, tmp_path: Path) -> None:
        real = _w(tmp_path, "r.xml", _junit(_tc("tests.test_a", "t1")))
        link = tmp_path / "l.xml"
        link.symlink_to(real)
        inv = _inv(tmp_path, 1, 0)
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(link),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 2
        )


class TestExecUnusedAllowlist:
    def test_warns(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "t1")))
        al = _al(
            tmp_path,
            [
                {
                    "node_id_pattern": "tests/gone.py::t",
                    "owner": "x",
                    "issue_url": "https://x/99",
                    "reason": "gone",
                    "created_at": "2026-08-09T00:00:00Z",
                    "expires_at": "2026-08-23T00:00:00Z",
                    "environments": ["ci"],
                }
            ],
        )
        r = tmp_path / "r.json"
        res = _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j),
                "--skip-allowlist",
                str(al),
                "--report",
                str(r),
            ],
        )
        assert res.returncode == 0
        assert "unused" in res.stderr.lower()


class TestExecOverbroad:
    def test_wildcard_dir_rejected(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "t1")))
        al = _al(
            tmp_path,
            [
                {
                    "node_id_pattern": "tests/*",
                    "owner": "x",
                    "issue_url": "https://x/1",
                    "reason": "bad",
                    "created_at": "2026-08-09T00:00:00Z",
                    "expires_at": "2026-08-23T00:00:00Z",
                    "environments": ["ci"],
                }
            ],
        )
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(al),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 2
        )


class TestExecMalformedAllowlist:
    def test_missing_fields(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "t1")))
        al = _w(
            tmp_path,
            "bad.json",
            json.dumps({"schema_version": 1, "entries": [{"node_id_pattern": "x"}]}),
        )
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(al),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 2
        )


class TestExecEnvironmentFilter:
    def test_wrong_env_not_matched(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 2, 1)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc("tests.integration.postgres.test_b", "t2", "skipped"),
            ),
        )
        al = _al(
            tmp_path,
            [
                {
                    "node_id_pattern": "tests/integration/postgres/test_b.py::t2",
                    "owner": "x",
                    "issue_url": "https://x/1",
                    "reason": "local only",
                    "created_at": "2026-08-09T00:00:00Z",
                    "expires_at": "2026-08-23T00:00:00Z",
                    "environments": ["local"],
                }
            ],
        )
        r = tmp_path / "r.json"
        res = _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j),
                "--skip-allowlist",
                str(al),
                "--environment",
                "ci",
                "--report",
                str(r),
            ],
        )
        assert res.returncode == 1
        assert "unexpected skip" in res.stderr.lower()


class TestExecClassMethod:
    def test_class_method_node(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a.TestClass", "test_method")))
        r = tmp_path / "r.json"
        res = _run(
            EXECUTION,
            [
                "--inventory",
                str(inv),
                "--junit",
                str(j),
                "--skip-allowlist",
                str(_al(tmp_path)),
                "--report",
                str(r),
            ],
        )
        assert res.returncode == 0
        report = json.loads(r.read_text())
        assert report["counts"]["passed"] == 1


class TestExecParamId:
    def test_param_in_name(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, 1, 0)
        j = _w(tmp_path, "j.xml", _junit(_tc("tests.test_a", "test_x[param-1]")))
        r = tmp_path / "r.json"
        assert (
            _run(
                EXECUTION,
                [
                    "--inventory",
                    str(inv),
                    "--junit",
                    str(j),
                    "--skip-allowlist",
                    str(_al(tmp_path)),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 0
        )
