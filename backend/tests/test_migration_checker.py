"""Offline regression tests for migration extension source policy."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
CHECKER = PROJECT_ROOT / "scripts" / "check-migrations.sh"

MIGRATION_TEMPLATE = '''"""synthetic migration"""
revision: str = "0001"
down_revision: str | None = None


def upgrade() -> None:
{upgrade}


def downgrade() -> None:
{downgrade}
'''

GIST = """    constraint = postgresql.ExcludeConstraint(
        ("resource_id", "="), name="ex_test", using="gist"
    )"""


def _write_fake_alembic(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -eu
case "$1" in
  history) printf '<base> -> 0001 (head), synthetic\\n' ;;
  heads) printf '0001 (head)\\n' ;;
  upgrade) printf 'CREATE TABLE synthetic (id INTEGER);\\n' ;;
  downgrade) printf 'DROP TABLE synthetic;\\n' ;;
  *) exit 2 ;;
esac
"""
    )
    path.chmod(0o755)


def _run_checker(
    tmp_path: Path, upgrade: str, downgrade: str = "    marker = True"
) -> subprocess.CompletedProcess[str]:
    backend = tmp_path / "backend"
    versions = backend / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_synthetic.py").write_text(
        MIGRATION_TEMPLATE.format(upgrade=upgrade, downgrade=downgrade)
    )
    fake_alembic = tmp_path / "alembic"
    _write_fake_alembic(fake_alembic)
    env = {
        **os.environ,
        "BACKEND_ROOT": str(backend),
        "VERSIONS_DIR": str(versions),
        "VENV_BIN": str(Path(sys.executable).parent),
        "ALEMBIC": str(fake_alembic),
    }
    return subprocess.run(
        ["bash", str(CHECKER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "statement",
    [
        'op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")',
        "op.execute('CREATE EXTENSION \"btree_gist\"')",
        'op.execute("cReAtE  \\n  ExTeNsIoN \\t IF NOT EXISTS \\n btree_gist ;")',
    ],
)
def test_approved_btree_gist_forms_pass(tmp_path: Path, statement: str) -> None:
    result = _run_checker(tmp_path, f"    {statement}\n{GIST}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CREATE EXTENSION 'btree_gist' is allowlisted" in result.stdout
    assert "CREATE EXTENSION ''" not in result.stdout


def test_unknown_extension_fails(tmp_path: Path) -> None:
    result = _run_checker(tmp_path, '    op.execute("CREATE EXTENSION hstore")')
    assert result.returncode == 1
    assert "0001_synthetic.py" in result.stderr
    assert "'hstore' is not allowlisted" in result.stderr


def test_multiple_approved_extension_occurrences_are_all_inspected(tmp_path: Path) -> None:
    upgrade = (
        '    op.execute("CREATE EXTENSION btree_gist")\n'
        "    op.execute('CREATE EXTENSION IF NOT EXISTS \"btree_gist\"')\n"
        f"{GIST}"
    )
    result = _run_checker(tmp_path, upgrade)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("CREATE EXTENSION 'btree_gist' is allowlisted") == 2


def test_one_approved_and_one_unknown_extension_fails(tmp_path: Path) -> None:
    upgrade = (
        '    op.execute("CREATE EXTENSION btree_gist")\n'
        '    op.execute("CREATE EXTENSION hstore")\n'
        f"{GIST}"
    )
    result = _run_checker(tmp_path, upgrade)
    assert result.returncode == 1
    assert "'btree_gist' is allowlisted" in result.stdout
    assert "'hstore' is not allowlisted" in result.stderr


@pytest.mark.parametrize(
    ("upgrade", "downgrade", "expected"),
    [
        (
            f'    op.execute("CREATE EXTENSION btree_gist")\n{GIST}',
            '    op.execute("DROP EXTENSION btree_gist")',
            "DROP EXTENSION is forbidden",
        ),
        (
            '    op.execute("CREATE EXTENSION btree_gist CASCADE")',
            "    marker = True",
            "unsupported CREATE EXTENSION statement",
        ),
        (
            '    extension = "btree_gist"\n    op.execute("CREATE EXTENSION " + extension)',
            "    marker = True",
            "dynamic or non-literal extension SQL is forbidden",
        ),
    ],
)
def test_dangerous_or_dynamic_extension_sql_fails(
    tmp_path: Path,
    upgrade: str,
    downgrade: str,
    expected: str,
) -> None:
    result = _run_checker(tmp_path, upgrade, downgrade)
    assert result.returncode == 1
    assert expected in result.stderr


def test_migration_without_extensions_passes(tmp_path: Path) -> None:
    result = _run_checker(tmp_path, "    value = 1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CREATE EXTENSION" not in result.stdout


def test_finding_is_safe_and_names_only_file_and_extension(tmp_path: Path) -> None:
    result = _run_checker(
        tmp_path,
        f'    op.execute("CREATE EXTENSION btree_gist")\n{GIST}',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    finding = next(line for line in result.stdout.splitlines() if "is allowlisted" in line)
    assert "0001_synthetic.py" in finding
    assert "btree_gist" in finding
    assert "fake_test_password" not in result.stdout + result.stderr


def test_btree_gist_created_after_exclusion_constraint_fails(tmp_path: Path) -> None:
    result = _run_checker(
        tmp_path,
        f'{GIST}\n    op.execute("CREATE EXTENSION btree_gist")',
    )
    assert result.returncode == 1
    assert "must be created before the GiST exclusion constraint" in result.stderr
