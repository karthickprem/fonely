"""Black-box and focused adversarial tests for migration policy enforcement."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
CHECKER = PROJECT_ROOT / "scripts" / "check-migrations.sh"
POLICY_PATH = PROJECT_ROOT / "scripts" / "migration_policy.py"
ALEMBIC = Path(sys.executable).with_name("alembic")
SPEC = importlib.util.spec_from_file_location("migration_policy", POLICY_PATH)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)

ENV_PY = """from alembic import context
context.configure(
    dialect_name="postgresql",
    literal_binds=True,
    dialect_opts={"paramstyle": "named"},
)
with context.begin_transaction():
    context.run_migrations()
"""


def migration(
    revision: str,
    parents: str | tuple[str, ...] | list[str] | None,
    upgrade: str = "    marker = True",
    downgrade: str = "    marker = True",
    *,
    branch_labels: object = None,
    depends_on: object = None,
    imports: str = "from alembic import op",
    module_prefix: str = "",
) -> str:
    return f'''"""synthetic migration"""
{imports}
{module_prefix}revision = {revision!r}
down_revision = {parents!r}
branch_labels = {branch_labels!r}
depends_on = {depends_on!r}

def upgrade():
{upgrade}

def downgrade():
{downgrade}
'''


def environment(tmp_path: Path, migrations: dict[str, str]) -> tuple[Path, Path]:
    backend = tmp_path / "backend"
    versions = backend / "migrations" / "versions"
    versions.mkdir(parents=True)
    (backend / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s/migrations\n")
    (backend / "migrations" / "env.py").write_text(ENV_PY)
    (backend / "migrations" / "script.py.mako").write_text("")
    venv = backend / ".venv"
    venv.symlink_to(Path(sys.executable).parents[1], target_is_directory=True)
    for name, source in migrations.items():
        path = versions / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return backend, versions


def run_checker(
    tmp_path: Path,
    migrations: dict[str, str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    backend, _ = environment(tmp_path, migrations)
    run_env = {**os.environ, **(env or {}), "BACKEND_ROOT": str(backend)}
    return subprocess.run(
        ["bash", str(CHECKER)],
        cwd=PROJECT_ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )


def scan(sql: str) -> Any:
    return policy.inspect_sql(sql)


class TestMetadataPolicy:
    @pytest.mark.parametrize("revision", ["0001", "Safe_A1", "a" * 32])
    def test_safe_revision_ids(self, tmp_path: Path, revision: str) -> None:
        path = tmp_path / "safe.py"
        path.write_text(migration(revision, None))
        assert policy.source_info(path).revision == revision

    @pytest.mark.parametrize(
        "revision", ["", "a" * 33, "base", "head", "heads", "current", "a:b", "a@b", "a+b", "a-b"]
    )
    def test_invalid_revision_ids(self, tmp_path: Path, revision: str) -> None:
        path = tmp_path / "invalid.py"
        path.write_text(migration(revision, None))
        with pytest.raises(policy.PolicyError, match=r"(?:invalid|reserved) revision"):
            policy.source_info(path)

    def test_missing_explicit_revision_does_not_use_filename(self, tmp_path: Path) -> None:
        path = tmp_path / "abcd.py"
        path.write_text(migration("abcd", None).replace("revision = 'abcd'\n", ""))
        with pytest.raises(policy.PolicyError, match="missing explicit migration metadata"):
            policy.source_info(path)

    @pytest.mark.parametrize(
        "body",
        [
            "    pass",
            '    "docstring"',
            "    True",
            "    False",
            "    None",
            "    ...",
            "    1",
            "    b'bytes'",
            "    []",
            "    {}",
            '    "docstring"\n    True\n    (1, 2)',
        ],
    )
    def test_empty_ordinary_bodies_fail(self, tmp_path: Path, body: str) -> None:
        path = tmp_path / "empty.py"
        path.write_text(migration("empty", None, body, body))
        with pytest.raises(policy.PolicyError, match="ordinary migration body is empty"):
            policy.source_info(path)

    @pytest.mark.parametrize(
        "labels",
        [("salon",), ["salon"], {"salon"}],
    )
    def test_safe_branch_labels(self, tmp_path: Path, labels: object) -> None:
        path = tmp_path / "labels.py"
        path.write_text(migration("labels", None, branch_labels=labels))
        assert policy.source_info(path).branch_labels == ("salon",)

    @pytest.mark.parametrize("labels", [1, ("",), ("head",), ("a@b",), ("dup", "dup")])
    def test_invalid_branch_labels(self, tmp_path: Path, labels: object) -> None:
        path = tmp_path / "labels.py"
        path.write_text(migration("labels", None, branch_labels=labels))
        with pytest.raises(policy.PolicyError, match="branch"):
            policy.source_info(path)


class TestSourceExecutionPolicy:
    def test_approved_direct_literal_upgrade(self, tmp_path: Path) -> None:
        path = tmp_path / "approved.py"
        path.write_text(
            migration(
                "approved",
                None,
                '    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")',
            )
        )
        info = policy.source_info(path)
        assert [(item.direction, item.name) for item in info.extensions] == [
            ("upgrade", "btree_gist")
        ]

    @pytest.mark.parametrize(
        "upgrade",
        [
            '    op.execute("CREATE EXTENSION hstore")',
            '    op.execute("CREATE/**/EXTENSION hstore")',
            '    op.execute("CREATE " + "EXT" + "ENSION hstore")',
            '    SQL = "CREATE EXTENSION btree_gist"\n    op.execute(SQL)',
            '    run = op.execute\n    run("CREATE EXTENSION btree_gist")',
            '    op.get_bind().execute("CREATE EXTENSION btree_gist")',
            '    op.get_bind().exec_driver_sql("CREATE EXTENSION btree_gist")',
            '    if False:\n        op.execute("CREATE EXTENSION btree_gist")',
            (
                "    if not context.is_offline_mode():\n"
                '        op.execute("CREATE EXTENSION btree_gist")'
            ),
        ],
    )
    def test_unsafe_extension_sources_fail(self, tmp_path: Path, upgrade: str) -> None:
        path = tmp_path / "unsafe.py"
        path.write_text(
            migration("unsafe", None, upgrade, imports="from alembic import op, context")
        )
        with pytest.raises(policy.PolicyError):
            policy.source_info(path)

    def test_keyword_form_execute_is_supported(self, tmp_path: Path) -> None:
        path = tmp_path / "keyword.py"
        path.write_text(
            migration(
                "keyword",
                None,
                '    op.execute(sqltext="CREATE EXTENSION btree_gist")',
            )
        )
        assert len(policy.source_info(path).extensions) == 1

    def test_extension_in_downgrade_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "down.py"
        path.write_text(
            migration("down", None, downgrade='    op.execute("CREATE EXTENSION btree_gist")')
        )
        with pytest.raises(policy.PolicyError, match="direct upgrade owner"):
            policy.source_info(path)

    def test_unused_extension_helper_fails(self, tmp_path: Path) -> None:
        prefix = 'def unused():\n    op.execute("CREATE EXTENSION btree_gist")\n\n'
        path = tmp_path / "helper.py"
        path.write_text(migration("helper", None, module_prefix=prefix))
        with pytest.raises(policy.PolicyError, match="no direct upgrade owner"):
            policy.source_info(path)

    def test_unresolved_non_extension_execution_fails_closed(self, tmp_path: Path) -> None:
        path = tmp_path / "dynamic.py"
        path.write_text(
            migration(
                "dynamic",
                None,
                '    op.execute(os.getenv("MIGRATION_SQL", "SELECT 1"))',
                imports="from alembic import op\nimport os",
            )
        )
        with pytest.raises(policy.PolicyError, match="unresolved database execution"):
            policy.source_info(path)

    @pytest.mark.parametrize(
        "upgrade",
        [
            "    conn = op.get_bind()\n    conn.execute(dynamic_sql)",
            "    arbitrary = op.get_bind()\n    other = arbitrary\n    other.execute(dynamic_sql)",
            "    conn = op.get_bind()\n    conn.exec_driver_sql(dynamic_sql)",
            "    conn = op.get_bind()\n    run = conn.execute\n    run(dynamic_sql)",
        ],
    )
    def test_get_bind_alias_unresolved_sql_fails(self, tmp_path: Path, upgrade: str) -> None:
        path = tmp_path / "bind_alias.py"
        path.write_text(migration("bind_alias", None, upgrade))
        with pytest.raises(policy.PolicyError, match="unresolved database execution"):
            policy.source_info(path)

    def test_safe_get_bind_alias_sql_passes(self, tmp_path: Path) -> None:
        path = tmp_path / "safe_bind.py"
        path.write_text(
            migration(
                "safe_bind",
                None,
                '    conn = op.get_bind()\n    conn.execute("SELECT 1")',
            )
        )
        assert policy.source_info(path).extensions == ()

    @pytest.mark.parametrize(
        ("call", "accepted"),
        [
            ('helper("SELECT 1")', True),
            ('helper(sql="SELECT 1")', True),
            ('helper(os.getenv("SQL"))', False),
            ('helper(sql=os.getenv("SQL"))', False),
            ('helper("SELECT 1")\n    helper(os.getenv("SQL"))', False),
            ('helper(sql="SELECT 1")\n    helper(os.getenv("SQL"))', False),
            ('helper("DROP EXTENSION btree_gist")', False),
            ('helper("CREATE EXTENSION btree_gist")', False),
            ('helper(sa.text("SELECT 1"))', True),
            ('helper(sql=sa.text("SELECT 1"))', True),
        ],
    )
    def test_helper_positional_keyword_parity(
        self, tmp_path: Path, call: str, accepted: bool
    ) -> None:
        path = tmp_path / "helper_parity.py"
        path.write_text(
            migration(
                "helper_parity",
                None,
                f"    {call}",
                imports="from alembic import op\nimport os\nimport sqlalchemy as sa",
                module_prefix="def helper(sql):\n    op.execute(sql)\n\n",
            )
        )
        if accepted:
            assert policy.source_info(path).extensions == ()
        else:
            with pytest.raises(policy.PolicyError):
                policy.source_info(path)

    def test_helper_forwarding_exact_provenance(self, tmp_path: Path) -> None:
        safe = tmp_path / "safe_forward.py"
        safe.write_text(
            migration(
                "safe_forward",
                None,
                '    outer("SELECT 1", "message")',
                module_prefix=(
                    "def inner(sql):\n    op.execute(sql)\n\n"
                    "def outer(sql, message):\n    inner(sql)\n\n"
                ),
            )
        )
        assert policy.source_info(safe).extensions == ()

        for name, prefix, call in (
            (
                "local_dynamic",
                "def helper(sql):\n    op.execute(sql)\n\n",
                '    dynamic_sql = os.getenv("SQL")\n    helper(dynamic_sql)',
            ),
            (
                "module_dynamic",
                "DYNAMIC_SQL = os.getenv('SQL')\n\ndef helper(sql):\n    op.execute(sql)\n\n",
                "    helper(DYNAMIC_SQL)",
            ),
            (
                "forward_dynamic",
                "def inner(sql):\n    op.execute(sql)\n\ndef outer(sql):\n    inner(sql)\n\n",
                '    outer("SELECT 1")\n    outer(os.getenv("SQL"))',
            ),
            (
                "recursive",
                "def helper(sql):\n    helper(sql)\n    op.execute(sql)\n\n",
                '    helper("SELECT 1")',
            ),
        ):
            path = tmp_path / f"{name}.py"
            path.write_text(
                migration(
                    name,
                    None,
                    call,
                    imports="from alembic import op\nimport os",
                    module_prefix=prefix,
                )
            )
            with pytest.raises(policy.PolicyError):
                policy.source_info(path)

    def test_helper_positional_keyword_public_wrapper(self, tmp_path: Path) -> None:
        accepted = run_checker(
            tmp_path,
            {
                "safe.py": migration(
                    "safe",
                    None,
                    '    helper("SELECT 1")\n    helper(sql="SELECT 2")',
                    module_prefix="def helper(sql):\n    op.execute(sql)\n\n",
                )
            },
        )
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr

        rejected_path = tmp_path / "rejected"
        rejected_path.mkdir()
        rejected = run_checker(
            rejected_path,
            {
                "unsafe.py": migration(
                    "unsafe",
                    None,
                    '    helper("SELECT 1")\n    helper(os.getenv("SQL"))',
                    imports="from alembic import op\nimport os",
                    module_prefix="def helper(sql):\n    op.execute(sql)\n\n",
                )
            },
        )
        assert rejected.returncode == 1
        output = rejected.stdout + rejected.stderr
        assert "MIGRATION_SQL" not in output
        assert "DROP EXTENSION" not in output
        assert "Traceback" not in output

    def test_helper_keyword_sql_binding(self, tmp_path: Path) -> None:
        safe = tmp_path / "safe_keyword.py"
        safe.write_text(
            migration(
                "safe_keyword",
                None,
                '    helper(sql="SELECT 1")',
                module_prefix="def helper(sql):\n    op.execute(sql)\n\n",
            )
        )
        assert policy.source_info(safe).extensions == ()

        unresolved = tmp_path / "unresolved_keyword.py"
        unresolved.write_text(
            migration(
                "unresolved_keyword",
                None,
                '    helper(sql=os.getenv("SQL"))',
                imports="from alembic import op\nimport os",
                module_prefix="def helper(sql):\n    op.execute(sql)\n\n",
            )
        )
        with pytest.raises(policy.PolicyError, match="unresolved database execution"):
            policy.source_info(unresolved)

    @pytest.mark.parametrize(
        "call",
        [
            'helper("SELECT 1", sql="SELECT 2")',
            'helper(unknown="SELECT 1")',
            "helper(*values)",
            "helper(**values)",
        ],
    )
    def test_unsupported_helper_call_binding_fails(self, tmp_path: Path, call: str) -> None:
        path = tmp_path / "bad_helper_call.py"
        path.write_text(
            migration(
                "bad_helper_call",
                None,
                f"    {call}",
                module_prefix="def helper(sql):\n    op.execute(sql)\n\n",
            )
        )
        with pytest.raises(
            policy.PolicyError,
            match=r"unresolved database execution|unsupported helper call binding",
        ):
            policy.source_info(path)

    def test_safe_non_extension_sa_text_passes(self, tmp_path: Path) -> None:
        path = tmp_path / "safe.py"
        path.write_text(
            migration(
                "safe",
                None,
                '    op.execute(sa.text("UPDATE widgets SET active = true"))',
                imports="from alembic import op\nimport sqlalchemy as sa",
            )
        )
        assert policy.source_info(path).extensions == ()

    @pytest.mark.parametrize(
        "call",
        ["op.execute()", 'op.execute("SELECT 1", "SELECT 2")', 'op.execute(statement="SELECT 1")'],
    )
    def test_malformed_execute_calls_fail(self, tmp_path: Path, call: str) -> None:
        path = tmp_path / "bad_call.py"
        path.write_text(migration("bad_call", None, f"    {call}"))
        with pytest.raises(policy.PolicyError, match="malformed database execution call"):
            policy.source_info(path)


class TestSqlPolicy:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT $$EXCLUDE USING gist$$;",
            "SELECT $tag$DROP EXTENSION btree_gist$tag$;",
            "SELECT E'EXCLUDE USING gist';",
            'SELECT "EXCLUDE USING gist";',
            "-- DROP EXTENSION btree_gist\rSELECT 1;",
            "/* outer /* DROP EXTENSION btree_gist */ comment */ SELECT 1;",
        ],
    )
    def test_inert_text_is_ignored(self, sql: str) -> None:
        evidence = scan(sql)
        assert evidence.extensions == ()
        assert evidence.exclusions == ()

    @pytest.mark.parametrize(
        "sql",
        [
            "DO $$ BEGIN EXECUTE 'CREATE EXTENSION hstore'; END $$;",
            "DO $$ BEGIN EXECUTE 'DROP EXT' || 'ENSION btree_gist'; END $$;",
            "DO $$ BEGIN EXECUTE format('CREATE EXTENSION %I', name); END $$;",
            (
                "CREATE FUNCTION f() RETURNS void AS $$ "
                "BEGIN DROP EXTENSION btree_gist; END $$ LANGUAGE plpgsql;"
            ),
            (
                "CREATE OR REPLACE PROCEDURE p() AS $$ "
                "BEGIN DROP EXTENSION btree_gist; END $$ LANGUAGE plpgsql;"
            ),
        ],
    )
    def test_procedural_extension_behavior_fails(self, sql: str) -> None:
        with pytest.raises(
            policy.PolicyError,
            match=r"procedural extension behavior|unsupported procedural SQL",
        ):
            scan(sql)

    def test_approved_perform_plus_other_execution_fails(self) -> None:
        for extra in (
            "CALL arbitrary_procedure();",
            "EXECUTE dynamic_sql;",
            "PERFORM arbitrary_function();",
        ):
            sql = (
                "DO $$ BEGIN "
                "PERFORM enforce_one_confirmed_appointment_allocation(1); "
                f"{extra} END $$;"
            )
            with pytest.raises(policy.PolicyError, match="unsupported procedural SQL"):
                scan(sql)

    def test_reviewed_table_lock_do_block_passes(self) -> None:
        scan(
            "DO $$ DECLARE table_name text; BEGIN "
            "FOREACH table_name IN ARRAY ARRAY['appointments'] LOOP "
            "IF to_regclass(format('%I.%I', current_schema(), table_name)) "
            "IS NOT NULL THEN EXECUTE format("
            "'LOCK TABLE %I.%I IN SHARE ROW EXCLUSIVE MODE', "
            "current_schema(), table_name); END IF; END LOOP; END $$;"
        )

    @pytest.mark.parametrize(
        "sql",
        [
            "DO $$ BEGIN EXECUTE chr(68) || chr(82); END $$;",
            "DO $$ BEGIN PERFORM arbitrary_function(); END $$;",
            "DO $$ BEGIN CALL arbitrary_procedure(); END $$;",
            "DO $$ BEGIN PERFORM dblink_exec('dbname=x', 'SELECT 1'); END $$;",
            ("DO $$ BEGIN PERFORM dblink_exec('dbname=x', 'CREATE EXTENSION hstore'); END $$;"),
        ],
    )
    def test_unsupported_procedural_execution_fails(self, sql: str) -> None:
        with pytest.raises(policy.PolicyError, match="unsupported procedural SQL"):
            scan(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SET standard_conforming_strings = off;",
            "RESET standard_conforming_strings;",
            "\\gexec\n",
            "  \\copy x to stdout\n",
        ],
    )
    def test_unsupported_client_or_string_modes_fail(self, sql: str) -> None:
        with pytest.raises(policy.PolicyError):
            scan(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            'CREATE EXTENSION "BTREE_GIST";',
            "CREATE EXTENSION btree_gist CASCADE;",
            "ALTER EXTENSION btree_gist UPDATE;",
            "DROP EXTENSION btree_gist;",
            "COMMENT ON EXTENSION btree_gist IS 'x';",
            "GRANT ALL ON EXTENSION btree_gist TO x;",
            "REVOKE ALL ON EXTENSION btree_gist FROM x;",
            "SECURITY LABEL ON EXTENSION btree_gist IS 'x';",
            "ALTER FUNCTION f() DEPENDS ON EXTENSION btree_gist;",
            "SELECT CREATE EXTENSION btree_gist;",
        ],
    )
    def test_forbidden_extension_grammar_is_classified(self, sql: str) -> None:
        try:
            evidence = scan(sql)
        except policy.PolicyError as exc:
            assert str(exc) == "ambiguous extension syntax"
            return
        assert evidence.extensions or pytest.fail("extension operation escaped classification")
        assert not (
            len(evidence.extensions) == 1
            and evidence.extensions[0].operation == "CREATE"
            and evidence.extensions[0].name == "btree_gist"
        )

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT EXCLUDE USING gist;",
            "DELETE FROM target AS exclude USING gist;",
            "MERGE INTO target AS exclude USING gist ON true WHEN MATCHED THEN DO NOTHING;",
        ],
    )
    def test_fake_exclusions_do_not_count(self, sql: str) -> None:
        assert scan(sql).exclusions == ()

    @pytest.mark.parametrize(
        "sql",
        [
            "ALTER TABLE ONLY IF EXISTS foo ADD CONSTRAINT ex EXCLUDE USING gist (id WITH =);",
            "ALTER TABLE IF ONLY foo ADD CONSTRAINT ex EXCLUDE USING gist (id WITH =);",
            "ALTER TABLE EXISTS foo ADD CONSTRAINT ex EXCLUDE USING gist (id WITH =);",
            "ALTER TABLE IF EXISTS IF EXISTS foo ADD CONSTRAINT ex EXCLUDE USING gist (id WITH =);",
            "ALTER TABLE ONLY ONLY foo ADD CONSTRAINT ex EXCLUDE USING gist (id WITH =);",
            "ALTER TABLE IF EXISTS ONLY ONLY foo ADD CONSTRAINT ex EXCLUDE USING gist (id WITH =);",
            "ALTER TABLE IF EXISTS ONLY ADD CONSTRAINT ex EXCLUDE USING gist (id WITH =);",
        ],
    )
    def test_invalid_alter_table_modifier_order_fails(self, sql: str) -> None:
        with pytest.raises(policy.PolicyError, match="ambiguous relation"):
            scan(sql)

    def test_relation_qualified_exclusion_lifecycle_is_ordered(self) -> None:
        evidence = scan(
            "ALTER TABLE a.t ADD CONSTRAINT ex EXCLUDE USING gist (id WITH =);"
            "ALTER TABLE a.t DROP CONSTRAINT ex;"
            "ALTER TABLE a.t ADD CONSTRAINT ex EXCLUDE USING gist (id WITH =);"
        )
        assert len(evidence.exclusions) == 2
        assert len(evidence.dropped_exclusions) == 1


class TestGraphAndBlackBox:
    def test_valid_merge_and_empty_merge_pass(self, tmp_path: Path) -> None:
        migrations = {
            "root.py": migration("root", None),
            "left.py": migration("left", "root"),
            "right.py": migration("right", "root"),
            "merge.py": migration("merge", ("left", "right"), "    pass", "    pass"),
        }
        result = run_checker(tmp_path, migrations)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Revisions accounted: 4" in result.stdout

    def test_operational_merge_passes(self, tmp_path: Path) -> None:
        migrations = {
            "root.py": migration("root", None),
            "left.py": migration("left", "root"),
            "right.py": migration("right", "root"),
            "merge.py": migration(
                "merge",
                ["left", "right"],
                '    op.execute("SELECT 1")',
                '    op.execute("SELECT 1")',
            ),
        }
        result = run_checker(tmp_path, migrations)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_dependency_converged_graph_passes(self, tmp_path: Path) -> None:
        migrations = {
            "root.py": migration("root", None),
            "dep.py": migration("dep", "root", branch_labels=("depbranch",)),
            "main.py": migration("main", "root", depends_on="dep"),
        }
        result = run_checker(tmp_path, migrations)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Effective head: main" in result.stdout

    def test_multiple_effective_heads_fail(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "root.py": migration("root", None),
                "left.py": migration("left", "root"),
                "right.py": migration("right", "root"),
            },
        )
        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr == "ERROR: migration policy helper process failed\n"

    def test_duplicate_revision_and_nested_unaccounted_file_fail(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "one.py": migration("dup", None),
                "nested/two.py": migration("dup", None),
            },
        )
        assert result.returncode == 1
        assert "dup" not in result.stderr

    def test_symlinked_migration_fails(self, tmp_path: Path) -> None:
        backend, versions = environment(tmp_path, {"root.py": migration("root", None)})
        (versions / "link.py").symlink_to(versions / "root.py")
        result = subprocess.run(
            ["bash", str(CHECKER)],
            cwd=PROJECT_ROOT,
            env={**os.environ, "BACKEND_ROOT": str(backend)},
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 1
        assert result.stderr == "ERROR: migration policy helper process failed\n"

    def test_import_output_is_sanitized(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "root.py": migration(
                    "root",
                    None,
                    module_prefix='print("fake-secret-import")\n',
                )
            },
        )
        assert result.returncode == 1
        assert "fake-secret-import" not in result.stdout + result.stderr

    def test_imported_branch_label_mutation_fails(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "root.py": migration(
                    "root",
                    None,
                    branch_labels=("static_label",),
                    module_prefix="branch_labels = ('mutated_label',)\n",
                )
            },
        )
        assert result.returncode == 1
        assert "mutated_label" not in result.stdout + result.stderr

    def test_inherited_branch_label_is_not_declared_child_label(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "root.py": migration("root", None, branch_labels=("root_branch",)),
                "child.py": migration("child", "root"),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_online_only_dynamic_extension_helper_fails(self, tmp_path: Path) -> None:
        helper = (
            "def install_extension():\n"
            "    sql = os.getenv('MIGRATION_SQL', 'SELECT 1')\n"
            "    op.execute(sql)\n\n"
        )
        result = run_checker(
            tmp_path,
            {
                "online.py": migration(
                    "online",
                    None,
                    "    if not context.is_offline_mode():\n        install_extension()",
                    imports="from alembic import op, context\nimport os",
                    module_prefix=helper,
                )
            },
        )
        assert result.returncode == 1
        assert "MIGRATION_SQL" not in result.stdout + result.stderr

    def test_poisoned_alembic_config_is_ignored(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {"root.py": migration("root", None)},
            env={"ALEMBIC_CONFIG": "/tmp/poisoned.ini"},
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_relative_backend_path_works(self, tmp_path: Path) -> None:
        backend, _ = environment(tmp_path, {"root.py": migration("root", None)})
        relative = os.path.relpath(backend, PROJECT_ROOT)
        result = subprocess.run(
            ["bash", str(CHECKER)],
            cwd=PROJECT_ROOT,
            env={**os.environ, "BACKEND_ROOT": relative},
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_cross_revision_extension_then_exclusion_passes(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "extension.py": migration(
                    "extension_owner",
                    None,
                    '    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")',
                ),
                "constraint.py": migration(
                    "constraint_owner",
                    "extension_owner",
                    '    op.execute("ALTER TABLE allocations ADD CONSTRAINT ex_alloc '
                    'EXCLUDE USING gist (resource_id WITH =)")',
                ),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "approved extension ownership validated" in result.stdout

    def test_surviving_exclusion_without_extension_fails(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "constraint.py": migration(
                    "constraint_owner",
                    None,
                    '    op.execute("ALTER TABLE allocations ADD CONSTRAINT ex_alloc '
                    'EXCLUDE USING gist (resource_id WITH =)")',
                )
            },
        )
        assert result.returncode == 1
        assert "EXCLUDE USING gist" not in result.stdout + result.stderr

    def test_no_extension_and_no_exclusion_passes(self, tmp_path: Path) -> None:
        result = run_checker(tmp_path, {"root.py": migration("root", None)})
        assert result.returncode == 0, result.stdout + result.stderr

    def test_unknown_extension_and_exclusion_fails(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "unknown.py": migration(
                    "unknown",
                    None,
                    '    op.execute("CREATE EXTENSION hstore")\n'
                    '    op.execute("ALTER TABLE allocations ADD CONSTRAINT ex_alloc '
                    'EXCLUDE USING gist (resource_id WITH =)")',
                )
            },
        )
        assert result.returncode == 1

    def test_early_and_late_surviving_exclusions_fail(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "early.py": migration(
                    "early",
                    None,
                    '    op.execute("ALTER TABLE early_table ADD CONSTRAINT ex_early '
                    'EXCLUDE USING gist (resource_id WITH =)")',
                ),
                "extension.py": migration(
                    "extension_owner",
                    "early",
                    '    op.execute("CREATE EXTENSION btree_gist")',
                ),
                "late.py": migration(
                    "late",
                    "extension_owner",
                    '    op.execute("ALTER TABLE late_table ADD CONSTRAINT ex_late '
                    'EXCLUDE USING gist (resource_id WITH =)")',
                ),
            },
        )
        assert result.returncode == 1

    def test_early_dropped_and_late_surviving_exclusion_passes(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "early.py": migration(
                    "early",
                    None,
                    '    op.execute("ALTER TABLE early_table ADD CONSTRAINT ex_early '
                    'EXCLUDE USING gist (resource_id WITH =)")',
                ),
                "extension.py": migration(
                    "extension_owner",
                    "early",
                    '    op.execute("ALTER TABLE early_table DROP CONSTRAINT ex_early")\n'
                    '    op.execute("CREATE EXTENSION btree_gist")',
                ),
                "late.py": migration(
                    "late",
                    "extension_owner",
                    '    op.execute("ALTER TABLE late_table ADD CONSTRAINT ex_late '
                    'EXCLUDE USING gist (resource_id WITH =)")',
                ),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_drop_table_retires_tracked_exclusion(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "owner.py": migration(
                    "owner",
                    None,
                    '    op.execute("CREATE EXTENSION btree_gist")\n'
                    '    op.execute("ALTER TABLE allocations ADD CONSTRAINT ex_alloc '
                    'EXCLUDE USING gist (resource_id WITH =)")\n'
                    '    op.execute("DROP TABLE allocations")',
                )
            },
        )
        assert result.returncode == 1

    def test_alter_table_if_exists_retires_constraint(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "owner.py": migration(
                    "owner",
                    None,
                    '    op.execute("CREATE EXTENSION btree_gist")\n'
                    '    op.execute("ALTER TABLE schema_name.allocations ADD CONSTRAINT ex_alloc '
                    'EXCLUDE USING gist (resource_id WITH =)")\n'
                    '    op.execute("ALTER TABLE IF EXISTS schema_name.allocations '
                    'DROP CONSTRAINT IF EXISTS ex_alloc")',
                )
            },
        )
        assert result.returncode == 1

    def test_extension_without_surviving_exclusion_fails(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "extension.py": migration(
                    "extension_owner",
                    None,
                    '    op.execute("CREATE EXTENSION btree_gist")',
                )
            },
        )
        assert result.returncode == 1

    def test_create_then_drop_only_exclusion_fails(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "extension.py": migration(
                    "extension_owner",
                    None,
                    '    op.execute("CREATE EXTENSION btree_gist")\n'
                    '    op.execute("ALTER TABLE allocations ADD CONSTRAINT ex_alloc '
                    'EXCLUDE USING gist (resource_id WITH =)")\n'
                    '    op.execute("ALTER TABLE allocations DROP CONSTRAINT ex_alloc")',
                )
            },
        )
        assert result.returncode == 1

    def test_downgrade_extension_creation_fails(self, tmp_path: Path) -> None:
        result = run_checker(
            tmp_path,
            {
                "root.py": migration(
                    "root",
                    None,
                    downgrade='    op.execute("CREATE EXTENSION hstore")',
                )
            },
        )
        assert result.returncode == 1


class TestWrapperProtocol:
    def wrapper_with_helper(
        self, tmp_path: Path, helper: str, timeout: str = "5"
    ) -> subprocess.CompletedProcess[str]:
        backend, _ = environment(tmp_path, {"root.py": migration("root", None)})
        replacement = tmp_path / "project"
        scripts = replacement / "scripts"
        scripts.mkdir(parents=True)
        wrapper = scripts / "check-migrations.sh"
        wrapper.write_text(CHECKER.read_text())
        wrapper.chmod(0o755)
        policy_helper = scripts / "migration_policy.py"
        policy_helper.write_text(helper)
        return subprocess.run(
            ["bash", str(wrapper)],
            cwd=replacement,
            env={
                **os.environ,
                "BACKEND_ROOT": str(backend),
                "MIGRATION_RENDER_TIMEOUT": timeout,
            },
            text=True,
            capture_output=True,
            check=False,
        )

    @pytest.mark.parametrize(
        "helper",
        [
            "raise SystemExit(1)\n",
            "print('not-json')\n",
            "print('{}')\n",
            "import sys; print('unexpected', file=sys.stderr); print('{}')\n",
            'print(\'{"protocol_version":1,"ok":false,"findings":[],"errors":["x"],"revision_count":1,"head":"h","evidence":{},"ddl_counts":{}}\')\n',
        ],
    )
    def test_helper_protocol_anomalies_fail(self, tmp_path: Path, helper: str) -> None:
        result = self.wrapper_with_helper(tmp_path, helper)
        assert result.returncode == 1
        assert result.stdout == ""
        assert "ERROR:" in result.stderr

    @pytest.mark.parametrize(
        "timeout",
        [
            "18446744073709551616",
            "9" * 200,
            "+1",
            " 1",
            "1.5",
        ],
    )
    def test_overflowing_or_malformed_timeout_fails_before_helper(
        self, tmp_path: Path, timeout: str
    ) -> None:
        result = self.wrapper_with_helper(
            tmp_path,
            "raise RuntimeError('helper-ran')\n",
            timeout=timeout,
        )
        assert result.returncode == 1
        assert "helper-ran" not in result.stdout + result.stderr
        assert "timeout" in result.stderr

    def test_helper_timeout_fails(self, tmp_path: Path) -> None:
        result = self.wrapper_with_helper(tmp_path, "import time; time.sleep(40)\n", timeout="1")
        assert result.returncode == 1
        assert result.stderr == "ERROR: migration policy helper process failed\n"

    @pytest.mark.parametrize("timeout", ["0", "-1", "invalid", "301"])
    def test_invalid_render_timeout_fails(self, tmp_path: Path, timeout: str) -> None:
        result = self.wrapper_with_helper(tmp_path, "print('{}')\n", timeout=timeout)
        assert result.returncode == 1
        assert "timeout" in result.stderr
        assert "Traceback" not in result.stderr
