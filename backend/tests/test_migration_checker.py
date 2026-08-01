"""Offline regression tests for migration extension policies."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
CHECKER = PROJECT_ROOT / "scripts" / "check-migrations.sh"

MIGRATION_TEMPLATE = '''"""synthetic migration"""
revision: str = {revision!r}
down_revision: str | None = {down_revision!r}


def upgrade() -> None:
{upgrade}


def downgrade() -> None:
{downgrade}
'''

SAFE_UPGRADE_SQL = """CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE appointment ADD CONSTRAINT ex_appointment EXCLUDE USING gist (resource_id WITH =);
"""
SAFE_DOWNGRADE_SQL = "ALTER TABLE appointment DROP CONSTRAINT ex_appointment;\n"


def _write_fake_alembic(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -eu
case "$1" in
  history) printf '%s\\n' "$FAKE_HISTORY" ;;
  heads) printf '%s\\n' "$FAKE_HEADS" ;;
  upgrade)
    if [[ "$2" == "head" ]]; then printf '%s\\n' "$FAKE_CUMULATIVE_UPGRADE_SQL"
    else printf '%s\\n' "$FAKE_REVISION_UPGRADE_SQL"
    fi ;;
  downgrade) printf '%s\\n' "$FAKE_REVISION_DOWNGRADE_SQL" ;;
  *) exit 2 ;;
esac
"""
    )
    path.chmod(0o755)


def _run_checker(
    tmp_path: Path,
    upgrade: str,
    downgrade: str = "    marker = True",
    *,
    revision: str = "extension_rev",
    down_revision: str | None = None,
    rendered_upgrade: str = SAFE_UPGRADE_SQL,
    rendered_downgrade: str = SAFE_DOWNGRADE_SQL,
) -> subprocess.CompletedProcess[str]:
    backend = tmp_path / "backend"
    versions = backend / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "synthetic.py").write_text(
        MIGRATION_TEMPLATE.format(
            revision=revision,
            down_revision=down_revision,
            upgrade=upgrade,
            downgrade=downgrade,
        )
    )
    fake_alembic = tmp_path / "alembic"
    _write_fake_alembic(fake_alembic)
    history_down = "<base>" if down_revision is None else down_revision
    env = {
        **os.environ,
        "BACKEND_ROOT": str(backend),
        "VERSIONS_DIR": str(versions),
        "VENV_BIN": str(Path(sys.executable).parent),
        "ALEMBIC": str(fake_alembic),
        "FAKE_HISTORY": (
            f"<base> -> {down_revision}, parent\n{history_down} -> {revision} (head), synthetic"
            if down_revision is not None
            else f"<base> -> {revision} (head), synthetic"
        ),
        "FAKE_HEADS": f"{revision} (head)",
        "FAKE_CUMULATIVE_UPGRADE_SQL": rendered_upgrade,
        "FAKE_REVISION_UPGRADE_SQL": rendered_upgrade,
        "FAKE_REVISION_DOWNGRADE_SQL": rendered_downgrade,
    }
    return subprocess.run(
        ["bash", str(CHECKER)], env=env, text=True, capture_output=True, check=False
    )


class TestExtensionSourceScanner:
    @pytest.mark.parametrize(
        "statement",
        [
            'op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")',
            "op.execute('CREATE EXTENSION \"btree_gist\"')",
            'op.execute("cReAtE  \\n  ExTeNsIoN \\t IF NOT EXISTS \\n btree_gist ;")',
        ],
    )
    def test_approved_literal_forms_pass(self, tmp_path: Path, statement: str) -> None:
        result = _run_checker(tmp_path, f"    {statement}")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "CREATE EXTENSION 'btree_gist' is allowlisted" in result.stdout

    def test_source_order_and_gist_presence_are_not_checked(self, tmp_path: Path) -> None:
        result = _run_checker(
            tmp_path,
            '    marker = "ExcludeConstraint before source SQL"\n'
            '    op.execute("CREATE EXTENSION btree_gist")',
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        ("upgrade", "expected"),
        [
            ('    op.execute("CREATE EXTENSION hstore")', "'hstore' is not allowlisted"),
            (
                '    op.execute("CREATE EXTENSION btree_gist CASCADE")',
                "unsupported CREATE EXTENSION statement",
            ),
            (
                '    extension = "btree_gist"\n    op.execute("CREATE EXTENSION " + extension)',
                "dynamic or non-literal extension SQL is forbidden",
            ),
            (
                '    op.execute("DROP EXTENSION btree_gist")',
                "DROP EXTENSION is forbidden",
            ),
        ],
    )
    def test_unsafe_extension_source_fails(
        self, tmp_path: Path, upgrade: str, expected: str
    ) -> None:
        result = _run_checker(tmp_path, upgrade)
        assert result.returncode == 1
        assert expected in result.stderr

    def test_multiple_source_occurrences_are_all_inspected(self, tmp_path: Path) -> None:
        result = _run_checker(
            tmp_path,
            '    op.execute("CREATE EXTENSION btree_gist")\n'
            '    op.execute("CREATE EXTENSION hstore")',
        )
        assert result.returncode == 1
        assert "'btree_gist' is allowlisted" in result.stdout
        assert "'hstore' is not allowlisted" in result.stderr

    def test_alias_execute_fails_safely(self, tmp_path: Path) -> None:
        result = _run_checker(
            tmp_path,
            '    operations.execute("CREATE EXTENSION btree_gist")',
        )
        assert result.returncode == 1
        assert "literal op.execute argument" in result.stderr

    def test_variable_literal_fails_safely(self, tmp_path: Path) -> None:
        result = _run_checker(
            tmp_path,
            '    SQL = "CREATE EXTENSION btree_gist"\n    op.execute(SQL)',
        )
        assert result.returncode == 1
        assert "literal op.execute argument" in result.stderr

    def test_migration_without_extensions_skips_rendered_policy(self, tmp_path: Path) -> None:
        result = _run_checker(
            tmp_path,
            "    value = 1",
            rendered_upgrade="DROP EXTENSION dangerous;",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "rendered extension SQL policy passed" not in result.stdout


class TestRenderedRevisionSQLPolicy:
    @pytest.mark.parametrize(
        ("revision", "down_revision"),
        [("extension_rev", None), ("feature_btree_gist", "parent-alpha")],
    )
    def test_exact_revision_sql_passes_for_base_and_nonnumeric_ids(
        self, tmp_path: Path, revision: str, down_revision: str | None
    ) -> None:
        result = _run_checker(
            tmp_path,
            '    op.execute("CREATE EXTENSION btree_gist")',
            revision=revision,
            down_revision=down_revision,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "rendered btree_gist creation precedes EXCLUDE USING gist" in result.stdout

    @pytest.mark.parametrize(
        ("rendered_upgrade", "expected"),
        [
            (
                "ALTER TABLE t ADD CONSTRAINT ex_t EXCLUDE USING gist (id WITH =);\n"
                "CREATE EXTENSION btree_gist;",
                "must precede EXCLUDE USING gist",
            ),
            ("CREATE EXTENSION btree_gist;", "has no EXCLUDE USING gist after btree_gist"),
            (
                "CREATE EXTENSION hstore;\n"
                "ALTER TABLE t ADD CONSTRAINT ex_t EXCLUDE USING gist (id WITH =);",
                "rendered unknown extension 'hstore'",
            ),
            (
                "CREATE EXTENSION btree_gist CASCADE;\n"
                "ALTER TABLE t ADD CONSTRAINT ex_t EXCLUDE USING gist (id WITH =);",
                "rendered upgrade contains unsupported extension SQL",
            ),
        ],
    )
    def test_rendered_upgrade_policy_fails(
        self, tmp_path: Path, rendered_upgrade: str, expected: str
    ) -> None:
        result = _run_checker(
            tmp_path,
            '    op.execute("CREATE EXTENSION btree_gist")',
            rendered_upgrade=rendered_upgrade,
        )
        assert result.returncode == 1
        assert expected in result.stderr

    def test_rendered_downgrade_cannot_drop_extension(self, tmp_path: Path) -> None:
        result = _run_checker(
            tmp_path,
            '    op.execute("CREATE EXTENSION btree_gist")',
            rendered_downgrade="DROP EXTENSION btree_gist;",
        )
        assert result.returncode == 1
        assert "rendered downgrade must not DROP EXTENSION" in result.stderr

    def test_exclusion_only_in_downgrade_does_not_satisfy_upgrade(self, tmp_path: Path) -> None:
        result = _run_checker(
            tmp_path,
            '    op.execute("CREATE EXTENSION btree_gist")',
            rendered_upgrade="CREATE EXTENSION btree_gist;",
            rendered_downgrade=(
                "ALTER TABLE t ADD CONSTRAINT ex_t EXCLUDE USING gist (id WITH =);"
            ),
        )
        assert result.returncode == 1
        assert "has no EXCLUDE USING gist" in result.stderr

    def test_comments_and_string_literals_do_not_count_as_exclusion(self, tmp_path: Path) -> None:
        result = _run_checker(
            tmp_path,
            '    op.execute("CREATE EXTENSION btree_gist")',
            rendered_upgrade=(
                "CREATE EXTENSION btree_gist;\n"
                "-- EXCLUDE USING gist (id WITH =)\n"
                "SELECT 'EXCLUDE USING gist';\n"
                "/* EXCLUDE USING gist */"
            ),
        )
        assert result.returncode == 1
        assert "has no EXCLUDE USING gist" in result.stderr

    def test_multiple_rendered_statements_normalize(self, tmp_path: Path) -> None:
        result = _run_checker(
            tmp_path,
            '    op.execute("CREATE EXTENSION btree_gist")',
            rendered_upgrade=(
                "CREATE TABLE service (id integer);\n"
                'CrEaTe ExTeNsIoN IF NOT EXISTS "btree_gist" ;\n'
                "ALTER TABLE t ADD CONSTRAINT ex_t\n"
                "EXCLUDE   USING   gist (id WITH =);"
            ),
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_errors_do_not_disclose_rendered_sql_or_credentials(self, tmp_path: Path) -> None:
        secret = "configured-secret-value"
        result = _run_checker(
            tmp_path,
            '    op.execute("CREATE EXTENSION btree_gist")',
            rendered_upgrade=f"-- {secret}\nCREATE EXTENSION btree_gist;",
        )
        assert result.returncode == 1
        assert "synthetic.py" in result.stderr
        assert "revision extension_rev" in result.stderr
        assert secret not in result.stdout + result.stderr
        assert "super-secret" not in result.stdout + result.stderr
