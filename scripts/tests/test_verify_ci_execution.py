"""Adversarial tests for CI execution-truth verifiers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PART = ROOT / "verify-test-partitions.py"
EXEC = ROOT / "verify-test-execution.py"


def run(script: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content)
    return p


def collect(nodes: list[str], *, total: int | None = None) -> str:
    selected = len(nodes)
    total = total if total is not None else selected
    if total != selected:
        footer = f"{selected}/{total} tests collected ({total - selected} deselected) in 1.0s"
    else:
        footer = f"{selected} tests collected in 1.0s"
    return "\n".join([*nodes, "", footer])


def tc(node: str, outcome: str = "passed") -> str:
    path, *rest = node.split("::")
    classname = path.removesuffix(".py").replace("/", ".")
    if len(rest) > 1:
        classname += "." + ".".join(rest[:-1])
    name = rest[-1]
    base = f'<testcase classname="{classname}" name="{name}">'
    prop = '<properties><property name="node_id" value="' + node + '"/></properties>'
    child = {
        "passed": "",
        "failed": '<failure message="fail"/>',
        "error": '<error message="err"/>',
        "skipped": '<skipped message="skip"/>',
        "xfail": '<skipped type="pytest.xfail" message="xfail"/>',
        "xpass": '<skipped type="pytest.xfail" message="[XPASS(strict)]"/>',
    }[outcome]
    return base + prop + child + "</testcase>"


def junit(cases: list[tuple[str, str]], **attrs: str) -> str:
    attr = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return (
        f'<testsuite tests="{len(cases)}" {attr}>'
        + "".join(tc(node, outcome) for node, outcome in cases)
        + "</testsuite>"
    )


def inventory(tmp: Path, npg: list[str], pg: list[str]) -> Path:
    import hashlib

    def digest(nodes: list[str]) -> str:
        return hashlib.sha256("\n".join(sorted(nodes)).encode()).hexdigest()

    all_nodes = sorted(set(npg) | set(pg))
    data = {
        "schema_version": 2,
        "valid": True,
        "errors": [],
        "counts": {"all": len(all_nodes), "non_pg": len(npg), "pg": len(pg)},
        "nodes": {"non_pg": npg, "pg": pg},
        "digests": {"all": digest(all_nodes), "non_pg": digest(npg), "pg": digest(pg)},
    }
    return write(tmp, "inventory.json", json.dumps(data))


def allowlist(tmp: Path, entries: list[dict] | None = None) -> Path:
    return write(tmp, "skips.json", json.dumps({"schema_version": 1, "entries": entries or []}))


def entry(pattern: str = "tests/test_a.py::test_a") -> dict:
    return {
        "node_id_pattern": pattern,
        "owner": "dev2",
        "reason": "known issue",
        "issue_url": "https://github.com/x/1",
        "created_at": "2026-08-09T00:00:00Z",
        "expires_at": "2026-08-20T00:00:00Z",
        "environments": ["ci"],
    }


def exec_args(
    tmp: Path,
    inv: Path,
    npg_cases: list[tuple[str, str]],
    pg_cases: list[tuple[str, str]],
    skips: Path | None = None,
) -> tuple[list[str], Path]:
    npg = write(tmp, "npg.xml", junit(npg_cases))
    pg = write(tmp, "pg.xml", junit(pg_cases))
    report = tmp / "report.json"
    return [
        "--inventory",
        str(inv),
        "--non-pg-junit",
        str(npg),
        "--pg-junit",
        str(pg),
        "--skip-allowlist",
        str(skips or allowlist(tmp)),
        "--environment",
        "ci",
        "--report",
        str(report),
    ], report


NPG = "tests/test_a.py::test_a"
PG = "tests/integration/postgres/test_b.py::test_b"


# 1-12 partition tests
@pytest.mark.parametrize("case", ["valid", "param-space", "class-node"])
def test_partition_valid_cases(tmp_path: Path, case: str) -> None:
    npg = [
        NPG
        if case == "valid"
        else (
            "tests/test_a.py::test_x[a b]"
            if case == "param-space"
            else "tests/test_a.py::TestA::test_a"
        )
    ]
    pg = [PG]
    paths = {
        k: write(tmp_path, f"{k}.txt", collect(v, total=len(npg) + len(pg) if k != "all" else None))
        for k, v in {"all": npg + pg, "npg": npg, "pg": pg}.items()
    }
    r = tmp_path / "r.json"
    assert (
        run(
            PART,
            [
                "--all",
                str(paths["all"]),
                "--non-pg",
                str(paths["npg"]),
                "--pg",
                str(paths["pg"]),
                "--report",
                str(r),
            ],
        ).returncode
        == 0
    )
    assert json.loads(r.read_text())["nodes"]["pg"] == pg


@pytest.mark.parametrize(
    "mode",
    [
        "overlap",
        "missing",
        "empty-pg",
        "duplicate",
        "no-footer",
        "collection-error",
        "footer-all",
        "footer-npg",
        "footer-pg",
    ],
)
def test_partition_invalid_cases(tmp_path: Path, mode: str) -> None:
    npg, pg = [NPG], [PG]
    alln = npg + pg
    if mode == "overlap":
        pg = [NPG]
    if mode == "missing":
        npg = []
    if mode == "empty-pg":
        pg = []
    if mode == "duplicate":
        alln = [NPG, NPG, PG]
    a = collect(alln)
    n = collect(npg, total=len(alln))
    p = collect(pg, total=len(alln))
    if mode == "no-footer":
        a = NPG
    if mode == "collection-error":
        a = "====== ERRORS ======\nERROR collecting tests/x.py\n0 tests collected in 1.0s"
    if mode == "footer-all":
        a = collect(alln, total=len(alln) + 1)
    if mode == "footer-npg":
        n = collect(npg, total=len(alln) + 1)
    if mode == "footer-pg":
        p = collect(pg, total=len(alln) + 1)
    r = tmp_path / "r.json"
    rc = run(
        PART,
        [
            "--all",
            str(write(tmp_path, "a.txt", a)),
            "--non-pg",
            str(write(tmp_path, "n.txt", n)),
            "--pg",
            str(write(tmp_path, "p.txt", p)),
            "--report",
            str(r),
        ],
    ).returncode
    assert rc != 0


# 13-27 execution outcome/membership tests
@pytest.mark.parametrize(
    "outcome,expected",
    [("passed", 0), ("failed", 1), ("error", 1), ("xfail", 0), ("xpass", 1), ("skipped", 1)],
)
def test_execution_outcomes(tmp_path: Path, outcome: str, expected: int) -> None:
    inv = inventory(tmp_path, [NPG], [PG])
    args, _ = exec_args(tmp_path, inv, [(NPG, outcome)], [(PG, "passed")])
    assert run(EXEC, args).returncode == expected


@pytest.mark.parametrize(
    "mode",
    [
        "missing-npg",
        "missing-pg",
        "extra-npg",
        "extra-pg",
        "swap",
        "duplicate-npg",
        "duplicate-pg",
        "zero-pg",
        "empty-all",
    ],
)
def test_execution_membership(tmp_path: Path, mode: str) -> None:
    inv = inventory(tmp_path, [NPG], [PG])
    npg, pg = [(NPG, "passed")], [(PG, "passed")]
    if mode == "missing-npg":
        npg = []
    if mode == "missing-pg":
        pg = []
    if mode == "extra-npg":
        npg.append(("tests/test_extra.py::x", "passed"))
    if mode == "extra-pg":
        pg.append(("tests/integration/postgres/test_extra.py::x", "passed"))
    if mode == "swap":
        npg, pg = [(PG, "passed")], [(NPG, "passed")]
    if mode == "duplicate-npg":
        npg.append((NPG, "passed"))
    if mode == "duplicate-pg":
        pg.append((PG, "passed"))
    if mode == "zero-pg":
        pg = []
    if mode == "empty-all":
        npg = []
        pg = []
    args, _ = exec_args(tmp_path, inv, npg, pg)
    assert run(EXEC, args).returncode != 0


# 28-37 allowlist governance
@pytest.mark.parametrize(
    "mode",
    [
        "allowed",
        "expired",
        "over-14d",
        "missing-field",
        "bad-env",
        "bad-url",
        "blank-owner",
        "naive-time",
        "reverse-time",
        "unused",
    ],
)
def test_allowlist_cases(tmp_path: Path, mode: str) -> None:
    e = entry(NPG)
    if mode == "expired":
        e.update(created_at="2026-07-01T00:00:00Z", expires_at="2026-07-10T00:00:00Z")
    if mode == "over-14d":
        e["expires_at"] = "2026-09-09T00:00:00Z"
    if mode == "missing-field":
        e.pop("issue_url")
    if mode == "bad-env":
        e["environments"] = ["prod"]
    if mode == "bad-url":
        e["issue_url"] = "not-url"
    if mode == "blank-owner":
        e["owner"] = ""
    if mode == "naive-time":
        e["created_at"] = "2026-08-09T00:00:00"
    if mode == "reverse-time":
        e["expires_at"] = "2026-08-01T00:00:00Z"
    if mode == "unused":
        e["node_id_pattern"] = "tests/gone.py::x"
    inv = inventory(tmp_path, [NPG], [PG])
    outcome = "skipped" if mode == "allowed" else "passed"
    args, _ = exec_args(tmp_path, inv, [(NPG, outcome)], [(PG, "passed")], allowlist(tmp_path, [e]))
    rc = run(EXEC, args).returncode
    assert rc == 0 if mode == "allowed" else rc != 0


# 38-45 security/artifact tests
def test_missing_allowlist_is_error(tmp_path: Path) -> None:
    inv = inventory(tmp_path, [NPG], [PG])
    args, _ = exec_args(
        tmp_path, inv, [(NPG, "passed")], [(PG, "passed")], tmp_path / "missing.json"
    )
    assert run(EXEC, args).returncode == 2


def test_malformed_xml(tmp_path: Path) -> None:
    inv = inventory(tmp_path, [NPG], [PG])
    bad = write(tmp_path, "npg.xml", "not xml")
    pg = write(tmp_path, "pg.xml", junit([(PG, "passed")]))
    r = tmp_path / "r.json"
    rc = run(
        EXEC,
        [
            "--inventory",
            str(inv),
            "--non-pg-junit",
            str(bad),
            "--pg-junit",
            str(pg),
            "--skip-allowlist",
            str(allowlist(tmp_path)),
            "--report",
            str(r),
        ],
    ).returncode
    assert rc == 2 and json.loads(r.read_text())["valid"] is False


def test_invalid_root_xml(tmp_path: Path) -> None:
    inv = inventory(tmp_path, [NPG], [PG])
    bad = write(tmp_path, "npg.xml", "<root><testcase name='x'/></root>")
    pg = write(tmp_path, "pg.xml", junit([(PG, "passed")]))
    r = tmp_path / "r.json"
    assert (
        run(
            EXEC,
            [
                "--inventory",
                str(inv),
                "--non-pg-junit",
                str(bad),
                "--pg-junit",
                str(pg),
                "--skip-allowlist",
                str(allowlist(tmp_path)),
                "--report",
                str(r),
            ],
        ).returncode
        == 2
    )


def test_suite_level_error_rejected(tmp_path: Path) -> None:
    inv = inventory(tmp_path, [NPG], [PG])
    bad = write(tmp_path, "npg.xml", '<testsuite tests="1" errors="1" failures="0"></testsuite>')
    pg = write(tmp_path, "pg.xml", junit([(PG, "passed")]))
    r = tmp_path / "r.json"
    assert (
        run(
            EXEC,
            [
                "--inventory",
                str(inv),
                "--non-pg-junit",
                str(bad),
                "--pg-junit",
                str(pg),
                "--skip-allowlist",
                str(allowlist(tmp_path)),
                "--report",
                str(r),
            ],
        ).returncode
        == 2
    )


def test_inventory_digest_mismatch(tmp_path: Path) -> None:
    inv = inventory(tmp_path, [NPG], [PG])
    d = json.loads(inv.read_text())
    d["digests"]["all"] = "bad"
    inv.write_text(json.dumps(d))
    args, _ = exec_args(tmp_path, inv, [(NPG, "passed")], [(PG, "passed")])
    assert run(EXEC, args).returncode == 2


def test_report_symlink_rejected(tmp_path: Path) -> None:
    inv = inventory(tmp_path, [NPG], [PG])
    target = write(tmp_path, "real.json", "{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    npg = write(tmp_path, "n.xml", junit([(NPG, "passed")]))
    pg = write(tmp_path, "p.xml", junit([(PG, "passed")]))
    rc = run(
        EXEC,
        [
            "--inventory",
            str(inv),
            "--non-pg-junit",
            str(npg),
            "--pg-junit",
            str(pg),
            "--skip-allowlist",
            str(allowlist(tmp_path)),
            "--report",
            str(link),
        ],
    ).returncode
    assert rc == 2


def test_hierarchical_junit_counted_once(tmp_path: Path) -> None:
    inv = inventory(tmp_path, [NPG], [PG])
    xml = f'<testsuites><testsuite tests="1">{tc(NPG)}</testsuite></testsuites>'
    npg = write(tmp_path, "n.xml", xml)
    pg = write(tmp_path, "p.xml", junit([(PG, "passed")]))
    r = tmp_path / "r.json"
    rc = run(
        EXEC,
        [
            "--inventory",
            str(inv),
            "--non-pg-junit",
            str(npg),
            "--pg-junit",
            str(pg),
            "--skip-allowlist",
            str(allowlist(tmp_path)),
            "--report",
            str(r),
        ],
    ).returncode
    assert rc == 0


def test_class_node_property_exact(tmp_path: Path) -> None:
    node = "tests/test_a.py::TestA::test_m"
    inv = inventory(tmp_path, [node], [PG])
    npg = write(tmp_path, "n.xml", junit([(node, "passed")]))
    pg = write(tmp_path, "p.xml", junit([(PG, "passed")]))
    r = tmp_path / "r.json"
    assert (
        run(
            EXEC,
            [
                "--inventory",
                str(inv),
                "--non-pg-junit",
                str(npg),
                "--pg-junit",
                str(pg),
                "--skip-allowlist",
                str(allowlist(tmp_path)),
                "--report",
                str(r),
            ],
        ).returncode
        == 0
    )
