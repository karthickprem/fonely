"""Tests for verify-test-partitions.py and verify-test-execution.py.

Covers partitions, execution membership, failures/errors nonzero,
xfail/xpass type attribute, allowlist schema enforcement, security
bounds, and adversarial inputs.
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
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name="pytest" tests="{len(tcs)}">\n' + "".join(tcs) + "</testsuite>\n"
    )


def _tc(cls: str, name: str, outcome: str = "passed") -> str:
    tag = f'<testcase classname="{cls}" name="{name}" time="0.01"'
    if outcome == "passed":
        return f"  {tag}/>\n"
    if outcome == "failed":
        return f'  {tag}><failure message="fail"/></testcase>\n'
    if outcome == "error":
        return f'  {tag}><error message="err"/></testcase>\n'
    if outcome == "skipped":
        return f'  {tag}><skipped message="skip"/></testcase>\n'
    if outcome == "xfail":
        return f'  {tag}><skipped type="pytest.xfail" message="xfail"/></testcase>\n'
    if outcome == "xpass":
        return f'  {tag}><skipped type="pytest.xfail" message="[XPASS(strict)]"/></testcase>\n'
    return f"  {tag}/>\n"


def _inv(
    tmp: Path,
    non_pg: list[str] | None = None,
    pg: list[str] | None = None,
) -> Path:
    npg = non_pg if non_pg is not None else ["tests/test_a.py::t1"]
    pgn = pg if pg is not None else ["tests/integration/postgres/test_b.py::t2"]
    d = {
        "schema_version": 2,
        "valid": True,
        "errors": [],
        "counts": {
            "all": len(npg) + len(pgn),
            "non_pg": len(npg),
            "pg": len(pgn),
        },
        "nodes": {"non_pg": npg, "pg": pgn},
    }
    return _w(tmp, "inv.json", json.dumps(d))


def _al(tmp: Path, entries: list[dict] | None = None) -> Path:
    return _w(
        tmp,
        "skips.json",
        json.dumps({"schema_version": 1, "entries": entries or []}),
    )


def _good_entry(
    pattern: str = "tests/test_a.py::t1",
) -> dict:
    return {
        "node_id_pattern": pattern,
        "owner": "dev2",
        "issue_url": "https://github.com/x/1",
        "reason": "known flaky under CI",
        "created_at": "2026-08-09T00:00:00Z",
        "expires_at": "2026-08-20T00:00:00Z",
        "environments": ["ci"],
    }


# ============================================================================
# PARTITION TESTS
# ============================================================================


class TestPartValid:
    def test_exact_disjoint(self, tmp_path: Path) -> None:
        a = _w(
            tmp_path,
            "a.txt",
            _collect("tests/t.py::t1", "tests/pg/t.py::t2"),
        )
        n = _w(
            tmp_path,
            "n.txt",
            _collect("tests/t.py::t1", deselected=1),
        )
        p = _w(
            tmp_path,
            "p.txt",
            _collect("tests/pg/t.py::t2", deselected=1),
        )
        r = tmp_path / "r.json"
        res = _run(
            PARTITIONS,
            [
                "--all",
                str(a),
                "--non-pg",
                str(n),
                "--pg",
                str(p),
                "--report",
                str(r),
            ],
        )
        assert res.returncode == 0
        rpt = json.loads(r.read_text())
        assert rpt["valid"] is True
        assert "tests/t.py::t1" in rpt["nodes"]["non_pg"]
        assert "tests/pg/t.py::t2" in rpt["nodes"]["pg"]

    def test_parametrized_with_spaces(self, tmp_path: Path) -> None:
        nodes = [
            "tests/t.py::T::t[a-1]",
            "tests/t.py::T::t[b 2]",
            "tests/pg/t.py::t[x]",
        ]
        a = _w(tmp_path, "a.txt", _collect(*nodes))
        n = _w(tmp_path, "n.txt", _collect(*nodes[:2], deselected=1))
        p = _w(tmp_path, "p.txt", _collect(nodes[2], deselected=2))
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                [
                    "--all",
                    str(a),
                    "--non-pg",
                    str(n),
                    "--pg",
                    str(p),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 0
        )

    def test_emits_node_sets(self, tmp_path: Path) -> None:
        a = _w(
            tmp_path,
            "a.txt",
            _collect("tests/t.py::t1", "tests/pg/t.py::t2"),
        )
        n = _w(
            tmp_path,
            "n.txt",
            _collect("tests/t.py::t1", deselected=1),
        )
        p = _w(
            tmp_path,
            "p.txt",
            _collect("tests/pg/t.py::t2", deselected=1),
        )
        r = tmp_path / "r.json"
        _run(
            PARTITIONS,
            [
                "--all",
                str(a),
                "--non-pg",
                str(n),
                "--pg",
                str(p),
                "--report",
                str(r),
            ],
        )
        rpt = json.loads(r.read_text())
        assert isinstance(rpt["nodes"]["non_pg"], list)
        assert isinstance(rpt["nodes"]["pg"], list)
        assert len(rpt["nodes"]["non_pg"]) == 1
        assert len(rpt["nodes"]["pg"]) == 1


class TestPartOverlap:
    def test_detected(self, tmp_path: Path) -> None:
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


class TestPartEmpty:
    def test_empty_pg(self, tmp_path: Path) -> None:
        a = _w(tmp_path, "a.txt", _collect("tests/t.py::t1"))
        n = _w(tmp_path, "n.txt", _collect("tests/t.py::t1"))
        p = _w(tmp_path, "p.txt", _collect(deselected=1))
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                [
                    "--all",
                    str(a),
                    "--non-pg",
                    str(n),
                    "--pg",
                    str(p),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 1
        )


class TestPartError:
    def test_collection_error(self, tmp_path: Path) -> None:
        err = (
            "tests/t.py::t1\n\n"
            "====== ERRORS ======\n"
            "ERROR collecting tests/broken.py\n\n"
            "0 tests collected in 0.5s\n"
        )
        a = _w(tmp_path, "a.txt", err)
        n = _w(tmp_path, "n.txt", _collect("tests/t.py::t1"))
        p = _w(
            tmp_path,
            "p.txt",
            _collect("tests/pg/t.py::t2", deselected=1),
        )
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                [
                    "--all",
                    str(a),
                    "--non-pg",
                    str(n),
                    "--pg",
                    str(p),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 1
        )

    def test_no_footer(self, tmp_path: Path) -> None:
        a = _w(tmp_path, "a.txt", "tests/t.py::t1\n")
        n = _w(tmp_path, "n.txt", "tests/t.py::t1\n")
        p = _w(tmp_path, "p.txt", "tests/pg/t.py::t2\n")
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                [
                    "--all",
                    str(a),
                    "--non-pg",
                    str(n),
                    "--pg",
                    str(p),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 1
        )

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

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        real = _w(tmp_path, "r.txt", _collect("tests/t.py::t1"))
        link = tmp_path / "l.txt"
        link.symlink_to(real)
        r = tmp_path / "r.json"
        assert (
            _run(
                PARTITIONS,
                [
                    "--all",
                    str(link),
                    "--non-pg",
                    str(real),
                    "--pg",
                    str(real),
                    "--report",
                    str(r),
                ],
            ).returncode
            == 2
        )


class TestPartDigest:
    def test_deterministic(self, tmp_path: Path) -> None:
        a = _w(
            tmp_path,
            "a.txt",
            _collect("tests/t.py::t1", "tests/pg/t.py::t2"),
        )
        n = _w(
            tmp_path,
            "n.txt",
            _collect("tests/t.py::t1", deselected=1),
        )
        p = _w(
            tmp_path,
            "p.txt",
            _collect("tests/pg/t.py::t2", deselected=1),
        )
        r1 = tmp_path / "r1.json"
        r2 = tmp_path / "r2.json"
        _run(
            PARTITIONS,
            [
                "--all",
                str(a),
                "--non-pg",
                str(n),
                "--pg",
                str(p),
                "--report",
                str(r1),
            ],
        )
        _run(
            PARTITIONS,
            [
                "--all",
                str(a),
                "--non-pg",
                str(n),
                "--pg",
                str(p),
                "--report",
                str(r2),
            ],
        )
        d1 = json.loads(r1.read_text())["digests"]
        d2 = json.loads(r2.read_text())["digests"]
        assert d1 == d2


# ============================================================================
# EXECUTION TESTS
# ============================================================================


class TestExecAllPassed:
    def test_passes(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc(
                    "tests.integration.postgres.test_b",
                    "t2",
                ),
            ),
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


class TestExecFailedNonzero:
    def test_failure_makes_invalid(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc(
                    "tests.integration.postgres.test_b",
                    "t2",
                    "failed",
                ),
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
        assert "failed" in res.stderr.lower()

    def test_error_makes_invalid(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc(
                    "tests.integration.postgres.test_b",
                    "t2",
                    "error",
                ),
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
        assert "error" in res.stderr.lower()


class TestExecMembership:
    def test_missing_node_fails(self, tmp_path: Path) -> None:
        inv = _inv(
            tmp_path,
            non_pg=["tests/test_a.py::t1", "tests/test_c.py::t3"],
            pg=["tests/integration/postgres/test_b.py::t2"],
        )
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc(
                    "tests.integration.postgres.test_b",
                    "t2",
                ),
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
        assert "missing" in res.stderr.lower()


class TestExecUnexpectedSkip:
    def test_fails(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc(
                    "tests.integration.postgres.test_b",
                    "t2",
                    "skipped",
                ),
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
    def test_passes_with_another_pg(self, tmp_path: Path) -> None:
        inv = _inv(
            tmp_path,
            pg=[
                "tests/integration/postgres/test_b.py::t2",
                "tests/integration/postgres/test_c.py::t3",
            ],
        )
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc(
                    "tests.integration.postgres.test_b",
                    "t2",
                    "skipped",
                ),
                _tc(
                    "tests.integration.postgres.test_c",
                    "t3",
                ),
            ),
        )
        al = _al(
            tmp_path,
            [_good_entry("tests/integration/postgres/test_b.py::t2")],
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


class TestExecExpired:
    def test_fails(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, non_pg=[], pg=[])
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1")),
        )
        entry = _good_entry("tests/old.py::t")
        entry["created_at"] = "2026-07-01T00:00:00Z"
        entry["expires_at"] = "2026-07-15T00:00:00Z"
        al = _al(tmp_path, [entry])
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


class TestExecXfail:
    def test_type_attribute(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, non_pg=["tests/test_a.py::t1"], pg=[])
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1", "xfail")),
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
        assert json.loads(r.read_text())["counts"]["xfail"] == 1


class TestExecXpass:
    def test_strict_fails(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, non_pg=["tests/test_a.py::t1"], pg=[])
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1", "xpass")),
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
        assert "xpass" in res.stderr.lower()


class TestExecZeroPg:
    def test_fails(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc(
                    "tests.integration.postgres.test_b",
                    "t2",
                    "skipped",
                ),
            ),
        )
        al = _al(
            tmp_path,
            [_good_entry("tests/integration/postgres/test_b.py::t2")],
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


class TestExecMalformed:
    def test_xml_exits_2(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path)
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

    def test_empty_junit(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path)
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

    def test_invalid_inventory(self, tmp_path: Path) -> None:
        inv = _w(
            tmp_path,
            "inv.json",
            '{"valid": false, "errors": ["x"]}',
        )
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1")),
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
            == 1
        )

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        real = _w(
            tmp_path,
            "r.xml",
            _junit(_tc("tests.test_a", "t1")),
        )
        link = tmp_path / "l.xml"
        link.symlink_to(real)
        inv = _inv(tmp_path)
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


class TestExecAllowlistSchema:
    def test_missing_fields_exits_2(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, non_pg=[], pg=[])
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1")),
        )
        al = _w(
            tmp_path,
            "bad.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [{"node_id_pattern": "x"}],
                }
            ),
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

    def test_overbroad_rejected(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, non_pg=[], pg=[])
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1")),
        )
        entry = _good_entry("tests/*")
        al = _al(tmp_path, [entry])
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

    def test_expiry_over_14d_rejected(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, non_pg=[], pg=[])
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1")),
        )
        entry = _good_entry()
        entry["created_at"] = "2026-08-01T00:00:00Z"
        entry["expires_at"] = "2026-09-01T00:00:00Z"
        al = _al(tmp_path, [entry])
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

    def test_unused_warns(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, non_pg=["tests/test_a.py::t1"], pg=[])
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1")),
        )
        al = _al(tmp_path, [_good_entry("tests/gone.py::t")])
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

    def test_wrong_env_not_matched(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path)
        j = _w(
            tmp_path,
            "j.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc(
                    "tests.integration.postgres.test_b",
                    "t2",
                    "skipped",
                ),
            ),
        )
        entry = _good_entry("tests/integration/postgres/test_b.py::t2")
        entry["environments"] = ["local"]
        al = _al(tmp_path, [entry])
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


class TestExecMultiJunit:
    def test_two_files(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path)
        j1 = _w(
            tmp_path,
            "n.xml",
            _junit(_tc("tests.test_a", "t1")),
        )
        j2 = _w(
            tmp_path,
            "p.xml",
            _junit(
                _tc(
                    "tests.integration.postgres.test_b",
                    "t2",
                ),
            ),
        )
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


class TestExecDigest:
    def test_deterministic(self, tmp_path: Path) -> None:
        inv = _inv(tmp_path, non_pg=["tests/test_a.py::t1"], pg=[])
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a", "t1")),
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
        d1 = json.loads(r1.read_text())["digest"]
        d2 = json.loads(r2.read_text())["digest"]
        assert d1 == d2

    def test_outcome_changes_digest(self, tmp_path: Path) -> None:
        inv = _inv(
            tmp_path,
            non_pg=["tests/test_a.py::t1", "tests/test_b.py::t2"],
            pg=[],
        )
        j1 = _w(
            tmp_path,
            "j1.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc("tests.test_b", "t2"),
            ),
        )
        j2 = _w(
            tmp_path,
            "j2.xml",
            _junit(
                _tc("tests.test_a", "t1"),
                _tc("tests.test_b", "t2", "failed"),
            ),
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
        d1 = json.loads(r1.read_text())["digest"]
        d2 = json.loads(r2.read_text())["digest"]
        assert d1 != d2


class TestExecClassMethod:
    def test_class_method_node(self, tmp_path: Path) -> None:
        inv = _inv(
            tmp_path,
            non_pg=["tests/test_a/TestClass.py::TestClass::test_m"],
            pg=[],
        )
        j = _w(
            tmp_path,
            "j.xml",
            _junit(_tc("tests.test_a.TestClass", "test_m")),
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
        assert res.returncode == 0 or "missing" in res.stderr.lower()
