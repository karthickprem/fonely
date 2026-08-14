"""Structural parity checks between ORM metadata and migrations through head.

The migrations are executed against a recording Alembic-op adapter. Captured
SQLAlchemy tables are compared to ORM metadata for columns, types, nullability,
server defaults, keys, checks, indexes, predicates, and exclusions. A live
PostgreSQL ``alembic check`` remains the final integration proof.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from fonely.domain.pending_actions.payloads import validate_payload
from fonely.domain.pending_actions.snapshots import payload_digest
from fonely.models.enums import PendingActionType
from fonely.models.schema import Base

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations" / "versions"
MIGRATION_0001 = MIGRATIONS_DIR / "0001_initial_schema.py"
MIGRATION_0002 = MIGRATIONS_DIR / "0002_pending_action_state_machine.py"
MIGRATION_0003 = MIGRATIONS_DIR / "0003_committed_entity_linkage.py"
MIGRATION_0004 = MIGRATIONS_DIR / "0004_appointment_engine.py"
MIGRATION_0005 = MIGRATIONS_DIR / "0005_inventory_order_engine.py"
MIGRATION_0006 = MIGRATIONS_DIR / "0006_business_onboarding.py"
MIGRATION_0007 = MIGRATIONS_DIR / "0007_notification_outbox.py"
MIGRATION_0008 = MIGRATIONS_DIR / "0008_conversation_persistence.py"
MIGRATION_0009 = MIGRATIONS_DIR / "0009_daily_context.py"
MIGRATION_0010 = MIGRATIONS_DIR / "0010_whatsapp_message_dedup.py"
MIGRATION_0011 = MIGRATIONS_DIR / "0011_conversation_turn_unique.py"
MIGRATION_0012 = MIGRATIONS_DIR / "0012_whatsapp_inbound_events.py"
MIGRATION_0013 = MIGRATIONS_DIR / "0013_whatsapp_inbound_event_corrections.py"
MIGRATION_0014 = MIGRATIONS_DIR / "0014_inbound_event_claim_and_dedup_removal.py"
MIGRATION_0015 = MIGRATIONS_DIR / "0015_notification_manifests.py"
MIGRATION_0016 = MIGRATIONS_DIR / "0016_business_whatsapp_channels.py"
MIGRATION_0017 = MIGRATIONS_DIR / "0017_channel_identities_and_call_sid.py"
MIGRATION_0018 = MIGRATIONS_DIR / "0018_dpdp_notice_evidence.py"
MIGRATION_0019 = MIGRATIONS_DIR / "0019_pending_action_callback_type.py"


class OperationRecorder:
    """Minimal Alembic Operations-compatible recorder for migration 0001."""

    def __init__(self) -> None:
        self.metadata = sa.MetaData()
        self.indexes: dict[str, set[tuple[str, tuple[str, ...], bool, str | None]]] = {}
        self.dropped_tables: list[str] = []
        self.executed_sql: list[str] = []
        self.operations: list[tuple[str, str]] = []

    def execute(self, statement: Any) -> None:
        rendered = str(statement)
        self.executed_sql.append(rendered)
        self.operations.append(("execute", rendered))

    def create_table(self, name: str, *elements: Any, **_: Any) -> sa.Table:
        self.operations.append(("create_table", name))
        return sa.Table(name, self.metadata, *elements)

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        unique: bool = False,
        **kwargs: Any,
    ) -> None:
        predicate = kwargs.get("postgresql_where")
        self.indexes.setdefault(table_name, set()).add(
            (
                name,
                tuple(columns),
                unique,
                " ".join(str(predicate).split()) if predicate is not None else None,
            )
        )

    def create_exclude_constraint(
        self,
        name: str,
        table_name: str,
        *elements: tuple[Any, str],
        **kwargs: Any,
    ) -> None:
        self.metadata.tables[table_name].append_constraint(
            postgresql.ExcludeConstraint(*elements, name=name, **kwargs)
        )

    def drop_index(self, name: str, table_name: str, **_: Any) -> None:
        indexes = self.indexes.setdefault(table_name, set())
        self.indexes[table_name] = {item for item in indexes if item[0] != name}

    def create_foreign_key(
        self,
        name: str,
        source_table: str,
        referent_table: str,
        local_cols: list[str],
        remote_cols: list[str],
        **kwargs: Any,
    ) -> None:
        table = self.metadata.tables[source_table]
        table.append_constraint(
            sa.ForeignKeyConstraint(
                local_cols,
                [f"{referent_table}.{column}" for column in remote_cols],
                name=name,
                deferrable=kwargs.get("deferrable"),
                initially=kwargs.get("initially"),
            )
        )

    def create_check_constraint(
        self,
        name: str,
        table_name: str,
        condition: str,
    ) -> None:
        self.metadata.tables[table_name].append_constraint(sa.CheckConstraint(condition, name=name))

    def create_unique_constraint(
        self,
        name: str,
        table_name: str,
        columns: list[str],
    ) -> None:
        table = self.metadata.tables[table_name]
        table.append_constraint(sa.UniqueConstraint(*columns, name=name))

    def drop_constraint(
        self,
        name: str,
        table_name: str,
        **_: Any,
    ) -> None:
        table = self.metadata.tables[table_name]
        constraint = next(
            (
                item
                for item in table.constraints
                if item.name == name
                or (
                    name == "operating_schedules_business_id_day_of_week_open_time_key"
                    and isinstance(item, sa.UniqueConstraint)
                    and tuple(column.name for column in item.columns)
                    == ("business_id", "day_of_week", "open_time")
                )
                or (
                    name == "schedule_exceptions_business_id_exception_date_key"
                    and isinstance(item, sa.UniqueConstraint)
                    and tuple(column.name for column in item.columns)
                    == ("business_id", "exception_date")
                )
                or (
                    name == "appointments_service_id_fkey"
                    and isinstance(item, sa.ForeignKeyConstraint)
                    and tuple(column.name for column in item.columns) == ("service_id",)
                )
                or (
                    name == "appointments_resource_id_fkey"
                    and isinstance(item, sa.ForeignKeyConstraint)
                    and tuple(column.name for column in item.columns) == ("resource_id",)
                )
                or (
                    name == "appointments_pending_action_id_fkey"
                    and isinstance(item, sa.ForeignKeyConstraint)
                    and tuple(column.name for column in item.columns) == ("pending_action_id",)
                )
                or (
                    name == "appointments_call_id_fkey"
                    and isinstance(item, sa.ForeignKeyConstraint)
                    and tuple(column.name for column in item.columns) == ("call_id",)
                )
                or (
                    name.endswith("_fkey")
                    and isinstance(item, sa.ForeignKeyConstraint)
                    and item.name is None
                    and self._fkey_name_matches_columns(name, table_name, item)
                )
            ),
            None,
        )
        if constraint is not None:
            table.constraints.remove(constraint)
            if isinstance(constraint, sa.ForeignKeyConstraint):
                for element in constraint.elements:
                    col = element.parent
                    col.foreign_keys.discard(element)

    @staticmethod
    def _fkey_name_matches_columns(
        name: str, table_name: str, constraint: sa.ForeignKeyConstraint
    ) -> bool:
        cols = tuple(column.name for column in constraint.columns)
        expected = f"{table_name}_{'_'.join(cols)}_fkey"
        return name == expected

    def drop_table(self, name: str, **_: Any) -> None:
        self.operations.append(("drop_table", name))
        self.dropped_tables.append(name)
        table = self.metadata.tables.get(name)
        if table is not None:
            self.metadata.remove(table)

    def add_column(self, table_name: str, column: sa.Column[Any]) -> None:
        self.operations.append(("add_column", f"{table_name}.{column.name}"))
        self.metadata.tables[table_name].append_column(column)

    def alter_column(self, table_name: str, column_name: str, **changes: Any) -> None:
        table = self.metadata.tables[table_name]
        column = table.c[column_name]
        if "nullable" in changes:
            column.nullable = changes["nullable"]
        if "type_" in changes:
            new_type = changes["type_"]
            column.type = new_type
            if isinstance(new_type, sa.Enum) and getattr(new_type, "create_constraint", False):
                constraint_name = new_type.name
                if constraint_name:
                    table.append_constraint(
                        sa.CheckConstraint(
                            column.in_(new_type.enums),
                            name=constraint_name,
                        )
                    )

    def drop_column(self, table_name: str, column_name: str) -> None:
        table = self.metadata.tables[table_name]
        table._columns.remove(table.c[column_name])


def _load_migration(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture_upgrade() -> OperationRecorder:
    recorder = OperationRecorder()
    for path, name in (
        (MIGRATION_0001, "fonely_migration_0001"),
        (MIGRATION_0002, "fonely_migration_0002"),
        (MIGRATION_0003, "fonely_migration_0003"),
        (MIGRATION_0004, "fonely_migration_0004"),
        (MIGRATION_0005, "fonely_migration_0005"),
        (MIGRATION_0006, "fonely_migration_0006"),
        (MIGRATION_0007, "fonely_migration_0007"),
        (MIGRATION_0008, "fonely_migration_0008"),
        (MIGRATION_0009, "fonely_migration_0009"),
        (MIGRATION_0010, "fonely_migration_0010"),
        (MIGRATION_0011, "fonely_migration_0011"),
        (MIGRATION_0012, "fonely_migration_0012"),
        (MIGRATION_0013, "fonely_migration_0013"),
        (MIGRATION_0014, "fonely_migration_0014"),
        (MIGRATION_0015, "fonely_migration_0015"),
        (MIGRATION_0016, "fonely_migration_0016"),
        (MIGRATION_0017, "fonely_migration_0017"),
        (MIGRATION_0018, "fonely_migration_0018"),
        # 0019 is intentionally NOT replayed here. It only DROPs and RECREATEs
        # the existing ``action_type`` CHECK constraint to add a value; it adds
        # no columns/tables. Replaying it would make this capture hold a literal
        # ``action_type IN (...6 values...)`` while the ORM models the same column
        # as a native-enum-style constraint rendered as POSTCOMPILE — a
        # representation mismatch, not a real drift. The constraint-value parity
        # 0019 DOES need (the migration's set equals PendingActionType) is asserted
        # directly in test_callback_constraint_matches_enum below.
    ):
        module = _load_migration(path, name)
        module.op = recorder
        if name in {"fonely_migration_0002", "fonely_migration_0004", "fonely_migration_0005"}:
            module.context = SimpleNamespace(is_offline_mode=lambda: True)
        module.upgrade()
    return recorder


def _capture_appointment_upgrade() -> OperationRecorder:
    recorder = _capture_upgrade()
    lock_index = next(
        index
        for index, operation in enumerate(recorder.operations)
        if operation[0] == "execute" and "LOCK TABLE" in operation[1]
    )
    recorder.operations = recorder.operations[lock_index:]
    return recorder


def _capture_downgrade() -> OperationRecorder:
    recorder = _capture_upgrade()
    recorder.dropped_tables.clear()
    recorder.operations.clear()
    module_0018 = _load_migration(MIGRATION_0018, "fonely_migration_0018_down")
    module_0018.op = recorder
    module_0018.context = SimpleNamespace(is_offline_mode=lambda: True)
    module_0018.downgrade()
    module_0017 = _load_migration(MIGRATION_0017, "fonely_migration_0017_down")
    module_0017.op = recorder
    module_0017.context = SimpleNamespace(is_offline_mode=lambda: True)
    module_0017.downgrade()
    module_0016 = _load_migration(MIGRATION_0016, "fonely_migration_0016_down")
    module_0016.op = recorder
    module_0016.context = SimpleNamespace(is_offline_mode=lambda: True)
    module_0016.downgrade()
    module_0015 = _load_migration(MIGRATION_0015, "fonely_migration_0015_down")
    module_0015.op = recorder
    module_0015.context = SimpleNamespace(is_offline_mode=lambda: True)
    module_0015.downgrade()
    module_0014 = _load_migration(MIGRATION_0014, "fonely_migration_0014_down")
    module_0014.op = recorder
    module_0014.downgrade()
    module_0013 = _load_migration(MIGRATION_0013, "fonely_migration_0013_down")
    module_0013.op = recorder
    module_0013.downgrade()
    module_0012 = _load_migration(MIGRATION_0012, "fonely_migration_0012_down")
    module_0012.op = recorder
    module_0012.downgrade()
    module_0011 = _load_migration(MIGRATION_0011, "fonely_migration_0011_down")
    module_0011.op = recorder
    module_0011.downgrade()
    module_0010 = _load_migration(MIGRATION_0010, "fonely_migration_0010_down")
    module_0010.op = recorder
    module_0010.downgrade()
    module_0009 = _load_migration(MIGRATION_0009, "fonely_migration_0009_down")
    module_0009.op = recorder
    module_0009.downgrade()
    module_0008 = _load_migration(MIGRATION_0008, "fonely_migration_0008_down")
    module_0008.op = recorder
    module_0008.downgrade()
    module_0007 = _load_migration(MIGRATION_0007, "fonely_migration_0007_down")
    module_0007.op = recorder
    module_0007.downgrade()
    module_0006 = _load_migration(MIGRATION_0006, "fonely_migration_0006_down")
    module_0006.op = recorder
    module_0006.downgrade()
    module_0005 = _load_migration(MIGRATION_0005, "fonely_migration_0005_down")
    module_0005.op = recorder
    module_0005.context = SimpleNamespace(is_offline_mode=lambda: True)
    module_0005.downgrade()
    module_0004 = _load_migration(MIGRATION_0004, "fonely_migration_0004_down")
    module_0004.op = recorder
    module_0004.context = SimpleNamespace(is_offline_mode=lambda: True)
    module_0004.downgrade()
    module_0003 = _load_migration(MIGRATION_0003, "fonely_migration_0003_down")
    module_0003.op = recorder
    module_0003.downgrade()
    module_0002 = _load_migration(MIGRATION_0002, "fonely_migration_0002_down")
    module_0002.op = recorder
    module_0002.downgrade()
    module_0001 = _load_migration(MIGRATION_0001, "fonely_migration_0001_down")
    module_0001.op = recorder
    module_0001.downgrade()
    return recorder


def _type_signature(column: sa.Column[Any]) -> str:
    return str(column.type.compile(dialect=postgresql.dialect())).upper()


def _fk_signatures(table: sa.Table) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    signatures = {
        (
            tuple(element.parent.name for element in constraint.elements),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.foreign_key_constraints
    }
    composite_local_columns = {
        column for local, _ in signatures if len(local) > 1 for column in local[1:]
    }
    return {
        signature
        for signature in signatures
        if not (len(signature[0]) == 1 and signature[0][0] in composite_local_columns)
    }


def _fk_deferrability_signatures(
    table: sa.Table,
) -> set[tuple[str | None, bool | None, str | None]]:
    return {
        (constraint.name, constraint.deferrable, constraint.initially)
        for constraint in table.foreign_key_constraints
        if constraint in table.constraints
    }


def _unique_signatures(table: sa.Table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }


def _normalize_postcompile(text: str) -> str:
    import re

    return re.sub(r"__\[POSTCOMPILE_\w+\]", "__[POSTCOMPILE]", text)


def _check_signatures(table: sa.Table) -> set[tuple[str | None, str]]:
    return {
        (constraint.name, _normalize_postcompile(" ".join(str(constraint.sqltext).split())))
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


def _orm_index_signatures(
    table: sa.Table,
) -> set[tuple[str, tuple[str, ...], bool, str | None]]:
    return {
        (
            index.name or "",
            tuple(expression.name for expression in index.expressions),
            index.unique,
            (
                " ".join(str(index.dialect_options["postgresql"]["where"]).split())
                if index.dialect_options["postgresql"]["where"] is not None
                else None
            ),
        )
        for index in table.indexes
    }


def _exclude_signatures(table: sa.Table) -> set[tuple[str, str, str, tuple[tuple[str, str], ...]]]:
    signatures = set()
    for constraint in table.constraints:
        if not isinstance(constraint, postgresql.ExcludeConstraint):
            continue
        expressions = []
        for expression, _, operator in constraint._render_exprs:
            normalized = str(expression).replace(f"{table.name}.", "")
            normalized = normalized.replace(":tstzrange_1", "'[)'")
            expressions.append((" ".join(normalized.split()), operator))
        signatures.add(
            (
                constraint.name or "",
                constraint.using,
                " ".join(str(constraint.where).split()),
                tuple(expressions),
            )
        )
    return signatures


def test_migration_and_orm_have_identical_application_tables() -> None:
    captured = _capture_upgrade()
    # The set equality is the assertion. A table-count literal here would be a
    # second copy of the same fact that has to be edited by every migration;
    # the explicit inventory lives once, in test_schema.EXPECTED_TABLES.
    assert set(captured.metadata.tables) == set(Base.metadata.tables)


def test_migration_and_orm_column_parity() -> None:
    captured = _capture_upgrade()
    for name, orm_table in Base.metadata.tables.items():
        migration_table = captured.metadata.tables[name]
        assert set(migration_table.columns.keys()) == set(orm_table.columns.keys()), name
        for column_name, orm_column in orm_table.columns.items():
            migration_column = migration_table.columns[column_name]
            assert migration_column.nullable == orm_column.nullable, f"{name}.{column_name}"
            assert migration_column.primary_key == orm_column.primary_key, f"{name}.{column_name}"
            assert _type_signature(migration_column) == _type_signature(orm_column), (
                f"{name}.{column_name}: migration={_type_signature(migration_column)} "
                f"orm={_type_signature(orm_column)}"
            )
            migration_default = (
                str(migration_column.server_default.arg)
                if migration_column.server_default
                else None
            )
            orm_default = str(orm_column.server_default.arg) if orm_column.server_default else None
            assert migration_default == orm_default, f"{name}.{column_name} server default"


def test_migration_and_orm_constraint_parity() -> None:
    captured = _capture_upgrade()
    for name, orm_table in Base.metadata.tables.items():
        migration_table = captured.metadata.tables[name]
        assert _fk_signatures(migration_table) == _fk_signatures(orm_table), name
        assert _fk_deferrability_signatures(migration_table) == _fk_deferrability_signatures(
            orm_table
        ), name
        assert _unique_signatures(migration_table) == _unique_signatures(orm_table), name
        assert _check_signatures(migration_table) == _check_signatures(orm_table), name


def test_migration_and_orm_index_parity() -> None:
    captured = _capture_upgrade()
    for name, orm_table in Base.metadata.tables.items():
        assert captured.indexes.get(name, set()) == _orm_index_signatures(orm_table), name


def test_migration_and_orm_exclusion_constraint_parity() -> None:
    captured = _capture_upgrade()
    migration = _exclude_signatures(captured.metadata.tables["resource_allocations"])
    orm = _exclude_signatures(Base.metadata.tables["resource_allocations"])
    expected = {
        (
            "ex_resource_allocations_active_overlap",
            "gist",
            "status = 'active'",
            (
                ("business_id", "="),
                ("resource_id", "="),
                ("tstzrange(effective_start_at, effective_end_at, '[)')", "&&"),
            ),
        )
    }
    assert migration == orm == expected


def test_migration_downgrade_drops_all_application_tables() -> None:
    recorder = _capture_downgrade()
    expected = set(Base.metadata.tables) | {"whatsapp_processed_messages"}
    assert set(recorder.dropped_tables) == expected
    # Drop order is FK-sensitive, so it is pinned explicitly and every new
    # migration is expected to extend this list at the front. Unlike a table
    # count, this is not a duplicate of a fact asserted elsewhere.
    assert recorder.dropped_tables[:9] == [
        "business_channel_identities",
        "business_whatsapp_channels",
        "notification_manifests",
        "whatsapp_delivery_attempts",
        "whatsapp_inbound_events",
        "whatsapp_processed_messages",
        "business_daily_context",
        "conversation_turns",
        "conversations",
    ]


def test_appointment_migration_installs_btree_gist_without_dropping_it() -> None:
    upgrade = _capture_upgrade()
    assert "CREATE EXTENSION IF NOT EXISTS btree_gist" in upgrade.executed_sql
    downgrade = _capture_downgrade()
    assert all("DROP EXTENSION" not in statement for statement in downgrade.executed_sql)


def test_appointment_upgrade_lock_precedes_preflight_guard_and_ddl() -> None:
    operations = _capture_appointment_upgrade().operations
    assert operations[0][0] == "execute"
    assert "LOCK TABLE" in operations[0][1]
    assert "SHARE ROW EXCLUSIVE MODE" in operations[0][1]
    assert "Migration 0004 requires online appointment preflight" in operations[1][1]
    assert operations.index(("add_column", "services.buffer_before_minutes")) > 1
    assert operations.index(("create_table", "service_resource_eligibility")) > 1


def test_appointment_downgrade_lock_precedes_guard_and_destruction() -> None:
    recorder = _capture_upgrade()
    recorder.operations.clear()
    migration = _load_migration(MIGRATION_0004, "fonely_migration_0004_lock_down")
    migration.op = recorder
    migration.context = SimpleNamespace(is_offline_mode=lambda: True)
    migration.downgrade()

    assert "LOCK TABLE" in recorder.operations[0][1]
    assert "SHARE ROW EXCLUSIVE MODE" in recorder.operations[0][1]
    assert "online representability preflight" in recorder.operations[1][1]
    assert recorder.operations.index(("drop_table", "appointment_commits")) > 1
    assert recorder.operations.index(("drop_table", "resource_allocations")) > 1
    assert recorder.operations.index(("drop_table", "service_resource_eligibility")) > 1


def test_upgrade_and_downgrade_use_one_deterministic_lock_order() -> None:
    source = MIGRATION_0004.read_text()
    expected = tuple(
        sorted(
            (
                "appointment_commits",
                "appointments",
                "businesses",
                "calls",
                "operating_schedules",
                "pending_actions",
                "resource_allocations",
                "resources",
                "schedule_exceptions",
                "service_resource_eligibility",
                "services",
            )
        )
    )
    migration = _load_migration(MIGRATION_0004, "fonely_migration_0004_lock_order")
    assert expected == migration._AFFECTED_TABLES
    assert source.count("_lock_affected_tables()") == 3


def test_appointment_preflight_covers_ambiguous_legacy_states() -> None:
    source = MIGRATION_0004.read_text()
    preflight = source[
        source.index("def _preflight_upgrade") : source.index("def _backfill_appointment_facts")
    ]
    for condition in (
        "b.timezone IN ('Factory', 'localtime', 'posixrules')",
        "pg_timezone_names",
        "b.timezone LIKE 'posix/%'",
        "b.timezone LIKE 'right/%'",
        "pending_action_id IS NULL",
        "p.business_id IS DISTINCT FROM a.business_id",
        "p.action_type IS DISTINCT FROM 'appointment'",
        "operation' IS DISTINCT FROM 'create'",
        "jsonb_typeof(p.proposed_payload)",
        "pg_input_is_valid",
        "start_at' !~* '(Z|[+-][0-9]{2}:[0-9]{2})$'",
        "end_at' !~* '(Z|[+-][0-9]{2}:[0-9]{2})$'",
        "effective_start_at' !~* '(Z|[+-][0-9]{2}:[0-9]{2})$'",
        "effective_end_at' !~* '(Z|[+-][0-9]{2}:[0-9]{2})$'",
        "status = 'held'",
        "s.business_id IS DISTINCT FROM a.business_id",
        "r.business_id IS DISTINCT FROM a.business_id",
        "p.business_id IS DISTINCT FROM a.business_id",
        "c.business_id IS DISTINCT FROM a.business_id",
        "a.end_at <= a.start_at",
        "close_time <= open_time",
        "inconsistent schedule exception",
        "overlapping capacity-bearing legacy appointments",
    ):
        assert condition in preflight
    assert source.index("_preflight_upgrade()") < source.index('op.execute("CREATE EXTENSION')
    assert source.index("_preflight_upgrade()") < source.index("_backfill_appointment_facts()")


def test_capacity_bearing_appointments_receive_allocations_during_backfill() -> None:
    source = MIGRATION_0004.read_text()
    allocation = source[source.index("def _backfill_allocations") : source.index("def upgrade")]
    assert "'confirmed', 'completed', 'no_show'" in allocation
    assert "cancelled" not in allocation


def test_overlap_diagnostic_precedes_allocation_insert_and_exclusion() -> None:
    source = MIGRATION_0004.read_text()
    assert (
        source.index("overlapping capacity-bearing legacy appointments")
        < source.index("INSERT INTO resource_allocations")
        < source.index("op.create_exclude_constraint(")
    )


def test_tenant_composite_foreign_key_signatures() -> None:
    captured = _capture_upgrade().metadata.tables
    allocation_fks = _fk_signatures(captured["resource_allocations"])
    assert (("business_id", "call_id"), ("calls.business_id", "calls.id")) in _fk_signatures(
        captured["appointments"]
    )
    assert (
        ("business_id", "appointment_id"),
        ("appointments.business_id", "appointments.id"),
    ) in allocation_fks
    allocation_fact_fk = next(
        constraint
        for constraint in captured["resource_allocations"].foreign_key_constraints
        if constraint.name == "fk_allocation_business_appointment"
    )
    assert allocation_fact_fk.deferrable is True
    assert allocation_fact_fk.initially == "DEFERRED"
    assert (
        ("business_id", "appointment_id", "pending_action_id"),
        ("appointments.business_id", "appointments.id", "appointments.pending_action_id"),
    ) in allocation_fks
    pending_action_fk = (
        ("business_id", "pending_action_id"),
        ("pending_actions.business_id", "pending_actions.id"),
    )
    assert pending_action_fk in _fk_signatures(captured["appointments"])
    assert pending_action_fk in _fk_signatures(captured["appointment_commits"])


def test_appointment_required_fact_nullability_and_source_check() -> None:
    table = _capture_upgrade().metadata.tables["appointments"]
    for column in (
        "service_id",
        "effective_start_at",
        "effective_end_at",
        "service_name_snapshot",
        "resource_name_snapshot",
        "duration_minutes_snapshot",
        "buffer_before_minutes_snapshot",
        "buffer_after_minutes_snapshot",
        "source",
        "updated_at",
    ):
        assert not table.c[column].nullable
    checks = dict(_check_signatures(table))
    assert checks["ck_appointment_source"] == (
        "source IN ('customer_conversation', 'owner_manual', 'walk_in')"
    )
    assert "make_interval" in checks["ck_appt_duration_arithmetic"]
    assert "make_interval" in checks["ck_appt_effective_arithmetic"]


def test_downgrade_preflight_precedes_destructive_operations() -> None:
    source = MIGRATION_0004.read_text()
    preflight = source.index("_preflight_downgrade()", source.index("def downgrade"))
    assert preflight < source.index('op.drop_table("appointment_commits")', preflight)
    for marker in (
        "resource-specific schedules",
        "resource-specific exceptions",
        "service-resource eligibility",
        "appointment mutation provenance",
        "modified resource allocations",
        "canonical appointment-allocation correspondence",
        "service buffers",
        "changed appointment facts",
    ):
        assert (
            marker
            in source[source.index("def _preflight_downgrade") : source.index("def downgrade")]
        )


def test_appointment_creation_provenance_is_validated_before_fk_replacement() -> None:
    source = MIGRATION_0004.read_text()
    preflight = source[source.index("def _preflight_upgrade") : source.index("def upgrade")]
    for evidence in (
        "p.business_id IS DISTINCT FROM a.business_id",
        "p.action_type IS DISTINCT FROM 'appointment'",
        "p.status IS DISTINCT FROM 'confirmed'",
        "p.committed_entity_type IS DISTINCT FROM 'appointment'",
        "p.committed_entity_id IS DISTINCT FROM a.id",
        "operation' IS DISTINCT FROM 'create'",
    ):
        assert evidence in preflight
    assert (
        source.index("p.business_id IS DISTINCT FROM a.business_id")
        < source.index('op.drop_constraint("appointments_pending_action_id_fkey"')
        < source.index('"fk_appointment_business_pending_action"')
    )


def test_appointment_provenance_nullable_and_unique() -> None:
    appointments = Base.metadata.tables["appointments"]
    assert appointments.c.pending_action_id.nullable
    assert ("pending_action_id",) in _unique_signatures(appointments)


def test_upgrade_preflight_fake_bind_rejects_each_state_before_ddl() -> None:
    migration = _load_migration(MIGRATION_0004, "fonely_migration_0004_fake_upgrade")
    checks = 14
    for failing_index in range(checks):
        results = []
        for index in range(failing_index + 1):
            result = Mock()
            result.scalar_one_or_none.return_value = 1 if index == failing_index else None
            results.append(result)
        bind = Mock()
        bind.execute.side_effect = results
        migration.op = SimpleNamespace(get_bind=lambda current_bind=bind: current_bind)

        with pytest.raises(RuntimeError, match="Migration 0004"):
            migration._preflight_upgrade()

        assert bind.execute.call_count == failing_index + 1


def test_upgrade_preflight_guards_every_provenance_cast_at_source() -> None:
    source = MIGRATION_0004.read_text()
    preflight = source[source.index("def _preflight_upgrade") : source.index("def upgrade")]
    for field, postgres_type in (
        ("service_id", "bigint"),
        ("resource_id", "bigint"),
        ("call_id", "bigint"),
        ("target_appointment_id", "bigint"),
        ("schema_version", "integer"),
        ("duration_minutes", "integer"),
        ("buffer_before_minutes", "integer"),
        ("buffer_after_minutes", "integer"),
        ("price", "numeric"),
        ("start_at", "timestamp with time zone"),
        ("end_at", "timestamp with time zone"),
        ("effective_start_at", "timestamp with time zone"),
        ("effective_end_at", "timestamp with time zone"),
    ):
        assert field in preflight
        assert postgres_type in preflight
    assert preflight.count("pg_input_is_valid") >= 18
    assert "CASE WHEN pg_input_is_valid" in preflight
    assert source.index("_preflight_upgrade()") < source.index('op.execute("CREATE EXTENSION')


def test_revision_chain_has_single_head() -> None:
    migrations = [
        _load_migration(path, f"fonely_chain_{path.stem}")
        for path in sorted(MIGRATIONS_DIR.glob("*.py"))
        if path.name != "__init__.py"
    ]
    revisions = {migration.revision for migration in migrations}
    parent_revisions = {
        migration.down_revision for migration in migrations if migration.down_revision is not None
    }
    heads = revisions - parent_revisions

    # Derived from disk, not hardcoded. A literal head has to be edited by
    # every migration that lands, and when one forgets, the failure reads as
    # "the chain is broken" rather than "the constant is stale" -- which is
    # exactly how 0017 landed with this file untouched. What is actually worth
    # asserting is the invariant: one head, and it is the highest revision
    # present.
    on_disk = sorted(revisions)
    assert heads == {on_disk[-1]}, f"expected a single head at {on_disk[-1]}, found {heads}"

    # Strictly sequential and unbranched: revision N descends from N-1, and
    # only the first has no parent. Expressed structurally for the same reason
    # -- it catches an accidental re-parent or a gap without going stale.
    expected_chain: dict[str, str | None] = {
        revision: (None if index == 0 else on_disk[index - 1])
        for index, revision in enumerate(on_disk)
    }
    assert {
        migration.revision: migration.down_revision for migration in migrations
    } == expected_chain

    # The numbering itself must be contiguous, which the chain above cannot
    # see: 0001, 0002, 0004 is a perfectly well-formed chain.
    assert on_disk == [f"{number:04d}" for number in range(1, len(on_disk) + 1)]


def test_callback_constraint_matches_enum() -> None:
    """0019's action_type CHECK value set must be DERIVED from PendingActionType,
    never a hand-typed list that could drift from the enum.

    0019 is not replayed in _capture_upgrade (it re-expresses an enum constraint
    the ORM renders as POSTCOMPILE, which that structural capture cannot compare).
    So the invariant it actually needs — the constraint mirrors the model exactly,
    upgrade includes every enum value and downgrade includes every value except
    callback — is asserted here directly against the migration module.
    """
    from fonely.models.enums import PendingActionType

    module = _load_migration(MIGRATION_0019, "fonely_migration_0019_parity")
    all_values = set(module._ALL_VALUES)
    without_callback = set(module._WITHOUT_CALLBACK)

    assert all_values == {member.value for member in PendingActionType}, (
        "0019 upgrade constraint must include EXACTLY the PendingActionType values "
        "(derived from the enum, not a literal list) so it cannot drift"
    )
    assert "callback" in all_values
    assert without_callback == all_values - {"callback"}, (
        "0019 downgrade constraint must be the enum value set minus 'callback'"
    )


def test_runtime_provenance_helpers_precede_and_simplify_create_trigger() -> None:
    source = MIGRATION_0004.read_text()
    facts_helper_start = source.index("CREATE FUNCTION appointment_payload_facts_match")
    call_id_helper_start = source.index("CREATE FUNCTION appointment_payload_call_id_matches(")
    create_start = source.index("CREATE FUNCTION enforce_appointment_provenance")
    create_end = source.index(
        "CREATE CONSTRAINT TRIGGER ck_customer_conversation_appointment_provenance"
    )
    call_id_helper = source[call_id_helper_start:create_start]
    create_function = source[create_start:create_end]

    assert facts_helper_start < call_id_helper_start < create_start
    assert (
        "appointment_payload_call_id_matches(\n"
        "               payload_data jsonb, expected_call_id bigint)"
    ) in call_id_helper
    assert "RETURNS boolean LANGUAGE plpgsql STABLE" in call_id_helper
    assert "STRICT" not in call_id_helper
    assert "RETURN COALESCE(parsed_call_id = expected_call_id, FALSE)" in call_id_helper
    assert "appointment_payload_facts_match(" in create_function
    assert "payload_data->'facts', NEW) IS NOT TRUE" in create_function
    assert "appointment_payload_call_id_matches(" in create_function
    assert "payload_data, NEW.call_id) IS NOT TRUE" in create_function
    assert "payload_data->'facts'->>'service_id'" not in create_function
    assert "payload_data->>'call_id'" not in create_function
    assert "CASE" not in create_function
    assert "::bigint" not in create_function
    assert "customer-conversation appointment requires matching" in create_function
    assert create_function.count("BEGIN") == 1
    assert create_function.count("END IF;") == 3
    assert create_function.count('$$"""') == 1


def test_runtime_provenance_casts_remain_fail_closed() -> None:
    source = MIGRATION_0004.read_text()
    runtime = source[
        source.index("CREATE FUNCTION appointment_payload_facts_match") : source.index(
            "def _preflight_downgrade"
        )
    ]
    for marker in (
        "pg_input_is_valid(facts->>'service_id', 'bigint')",
        "pg_input_is_valid(facts->>'resource_id', 'bigint')",
        "facts->>'start_at', 'timestamp with time zone'",
        "facts->>'end_at', 'timestamp with time zone'",
        "parsed_call_id := call_id_text::bigint",
        "parsed_value := field_text::bigint",
    ):
        assert marker in runtime
    assert "RETURN COALESCE(" in runtime
    assert "IS NOT TRUE" in runtime


def test_positive_integer_helper_precedes_and_simplifies_dependents() -> None:
    source = MIGRATION_0004.read_text()
    helper_start = source.index("CREATE FUNCTION appointment_payload_positive_integer_matches(")
    create_commit_start = source.index("CREATE FUNCTION enforce_appointment_commit_provenance")
    create_commit_end = source.index("CREATE CONSTRAINT TRIGGER ck_appointment_commit_provenance")
    mutation_start = source.index("CREATE FUNCTION enforce_appointment_mutation_commit")
    mutation_end = source.index("CREATE CONSTRAINT TRIGGER ck_appointment_mutation_commit")
    helper = source[helper_start : source.index("CREATE FUNCTION enforce_appointment_provenance")]
    commit_function = source[create_commit_start:create_commit_end]
    mutation_function = source[mutation_start:mutation_end]

    assert helper_start < create_commit_start < mutation_start
    assert (
        "appointment_payload_positive_integer_matches(\n"
        "               payload_data jsonb, field_name text, expected_value integer)"
    ) in helper
    assert "RETURNS boolean LANGUAGE plpgsql STABLE" in helper
    assert "STRICT" not in helper
    assert "RETURN COALESCE(parsed_value = expected_value, FALSE)" in helper
    assert commit_function.count("appointment_payload_positive_integer_matches(") == 2
    assert mutation_function.count("appointment_payload_positive_integer_matches(") == 2
    for function in (commit_function, mutation_function):
        assert "CASE WHEN pg_input_is_valid" not in function
        assert "target_appointment_id')::bigint" not in function
        assert "target_expected_version')::integer" not in function


def test_provenance_helper_downgrade_order_is_dependency_safe() -> None:
    source = MIGRATION_0004.read_text()
    downgrade = source[source.index("def downgrade") :]
    mutation_trigger_drop = downgrade.index("DROP TRIGGER ck_appointment_mutation_commit")
    mutation_function_drop = downgrade.index("DROP FUNCTION enforce_appointment_mutation_commit()")
    commit_trigger_drop = downgrade.index("DROP TRIGGER ck_appointment_commit_provenance")
    commit_function_drop = downgrade.index("DROP FUNCTION enforce_appointment_commit_provenance()")
    positive_integer_helper_drop = downgrade.index(
        "DROP FUNCTION appointment_payload_positive_integer_matches(jsonb, text, integer)"
    )
    provenance_trigger_drop = downgrade.index(
        "DROP TRIGGER ck_customer_conversation_appointment_provenance"
    )
    provenance_function_drop = downgrade.index("DROP FUNCTION enforce_appointment_provenance()")
    call_id_helper_drop = downgrade.index(
        "DROP FUNCTION appointment_payload_call_id_matches(jsonb, bigint)"
    )
    facts_helper_drop = downgrade.index(
        "DROP FUNCTION appointment_payload_facts_match(jsonb, appointments)"
    )
    assert (
        mutation_trigger_drop
        < mutation_function_drop
        < commit_trigger_drop
        < commit_function_drop
        < positive_integer_helper_drop
    )
    assert (
        provenance_trigger_drop < provenance_function_drop < call_id_helper_drop < facts_helper_drop
    )


def test_downgrade_preflight_fake_bind_rejects_each_lossy_state() -> None:
    migration = _load_migration(MIGRATION_0004, "fonely_migration_0004_fake_downgrade")
    checks = 8
    for failing_index in range(checks):
        results = []
        for index in range(failing_index + 1):
            result = Mock()
            result.scalar_one_or_none.return_value = 1 if index == failing_index else None
            results.append(result)
        bind = Mock()
        bind.execute.side_effect = results
        migration.op = SimpleNamespace(get_bind=lambda current_bind=bind: current_bind)

        with pytest.raises(RuntimeError, match="Migration 0004 downgrade"):
            migration._preflight_downgrade()

        assert bind.execute.call_count == failing_index + 1


def test_downgrade_capacity_status_matches_upgrade_backfill() -> None:
    source = MIGRATION_0004.read_text()
    capacity_statuses = "'confirmed', 'completed', 'no_show'"
    backfill_section = source[
        source.index("def _backfill_allocations") : source.index("def upgrade")
    ]
    assert capacity_statuses in backfill_section

    downgrade_section = source[
        source.index("def _preflight_downgrade") : source.index("def downgrade")
    ]
    assert "a.status NOT IN ('confirmed', 'completed', 'no_show')" in downgrade_section
    assert "a.status IN ('confirmed', 'completed', 'no_show')" in downgrade_section
    assert "a.status IS DISTINCT FROM 'confirmed'" not in downgrade_section
    assert "a.status <> 'confirmed'" not in downgrade_section


def test_capacity_allocation_backfill_flushes_named_fk_before_exclusion() -> None:
    source = MIGRATION_0004.read_text()
    online_block_start = source.index(
        "if not context.is_offline_mode():\n        _backfill_allocations()"
    )
    backfill = source.index("_backfill_allocations()", online_block_start)
    flush = source.index(
        'op.execute("SET CONSTRAINTS fk_allocation_business_appointment IMMEDIATE")',
        backfill,
    )
    exclusion = source.index("op.create_exclude_constraint(", flush)
    online_block = source[online_block_start:exclusion]

    assert backfill < flush < exclusion
    assert "if not context.is_offline_mode():" in online_block
    assert "SET CONSTRAINTS ALL" not in online_block


def test_capacity_allocation_deferred_trigger_lifecycle_is_rendered() -> None:
    upgrade = _capture_upgrade().executed_sql
    source = MIGRATION_0004.read_text()
    for marker in (
        "CREATE FUNCTION enforce_one_confirmed_appointment_allocation",
        "CREATE FUNCTION enforce_confirmed_appointment_allocation",
        "CREATE CONSTRAINT TRIGGER ck_confirmed_appointment_active_allocation_from_appointment",
        "CREATE CONSTRAINT TRIGGER ck_confirmed_appointment_active_allocation_from_allocation",
        "AFTER INSERT OR UPDATE OR DELETE ON appointments",
        "AFTER INSERT OR UPDATE OR DELETE ON resource_allocations",
        "DEFERRABLE INITIALLY DEFERRED",
        "appointment requires exactly one",
        "cancelled appointment requires zero active allocations",
    ):
        assert any(marker in statement for statement in upgrade)
    deferred = [s for s in upgrade if "CONSTRAINT TRIGGER" in s]
    assert all("UPDATE OF" not in s for s in deferred)
    consistency_function = next(
        statement
        for statement in upgrade
        if "CREATE FUNCTION enforce_one_confirmed_appointment_allocation" in statement
    )
    assert "appointment_status IN ('confirmed', 'completed', 'no_show')" in consistency_function
    downgrade = source[source.index("def downgrade") :]
    assert downgrade.index("DROP TRIGGER") < downgrade.index(
        'op.drop_table("resource_allocations")'
    )
    assert "DROP FUNCTION enforce_confirmed_appointment_allocation()" in downgrade
    assert (
        "DROP FUNCTION enforce_one_confirmed_appointment_allocation(integer, integer)" in downgrade
    )


def test_integrity_trigger_and_append_only_sql_is_rendered() -> None:
    rendered = "\n".join(_capture_upgrade().executed_sql)
    for marker in (
        "ck_customer_conversation_appointment_provenance",
        "ck_committed_pending_action_provenance",
        "committed PendingAction provenance is immutable",
        "ck_appointment_commit_provenance",
        "ck_appointment_mutation_commit",
        "ck_confirmed_appointment_action_commit",
        "appointment_authoritative_snapshot",
        "appointment_payload_facts_match",
        "appointment commits are append-only",
        "CONSTRAINT = 'ck_appointment_commit_append_only'",
        "ck_resource_allocation_immutable_identity",
        "resource allocations cannot be deleted",
        "NEW.version <> OLD.version + 1",
    ):
        assert marker in rendered


def test_upgrade_preflight_is_null_safe_and_service_wide() -> None:
    source = MIGRATION_0004.read_text()
    preflight = source[source.index("def _preflight_upgrade") : source.index("def upgrade")]
    assert "SELECT 1 FROM services" in preflight
    assert "duration_minutes IS NULL" in preflight
    assert "COALESCE(f.facts->>'service_id' !~" in preflight
    assert "NOT COALESCE(pg_input_is_valid" in preflight
    first_guard = preflight.index("pg_input_is_valid(f.facts->>'service_id', 'bigint')")
    first_cast = preflight.index("(f.facts->>'service_id')::bigint")
    assert first_guard < first_cast


def test_downgrade_restores_scalar_appointment_provenance_fk() -> None:
    source = MIGRATION_0004.read_text()
    downgrade = source[source.index("def downgrade") :]
    drop_composite = downgrade.index("fk_appointment_business_pending_action")
    restore_scalar = downgrade.index("appointments_pending_action_id_fkey")
    drop_tenant_unique = downgrade.index("uq_pending_actions_business_id_id")
    assert drop_composite < restore_scalar < drop_tenant_unique


def test_phase_b_offline_migration_guards_populated_database() -> None:
    recorder = _capture_upgrade()
    assert any("requires online backfill" in statement for statement in recorder.executed_sql)


def _assert_migration_digest_matches_runtime(
    action_type: PendingActionType,
    raw_payload: dict[str, object],
) -> None:
    runtime = validate_payload(action_type, 1, raw_payload)
    migration = _load_migration(MIGRATION_0002, "fonely_migration_0002_digest")
    assert migration._digest(action_type.value, 1, raw_payload) == payload_digest(runtime)


def test_phase_b_order_migration_digest_matches_runtime_canonicalizer() -> None:
    raw_payload = {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_name": "X" * 200,
            "customer_phone": "+919123456789",
            "pickup_at": "2026-08-01T15:30:00+05:30",
            "lines": [
                {"product_id": 2, "quantity": "2.00"},
                {"product_id": 1, "quantity": "1"},
            ],
            "customer_note": "N" * 500,
        },
    }
    _assert_migration_digest_matches_runtime(PendingActionType.ORDER, raw_payload)


def test_phase_b_order_optional_none_digest_matches_runtime() -> None:
    raw_payload = {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_name": None,
            "customer_phone": "+919123456789",
            "pickup_at": "2026-08-01T10:00:00Z",
            "lines": [{"product_id": 1, "quantity": "2.00"}],
            "customer_note": None,
        },
    }
    _assert_migration_digest_matches_runtime(PendingActionType.ORDER, raw_payload)


def test_phase_b_stock_update_digest_matches_runtime() -> None:
    raw_payload = {
        "schema_version": 1,
        "action_type": "owner_stock_update",
        "data": {
            "product_id": 7,
            "business_date": "2026-08-01",
            "operation": "add",
            "quantity": "5.00",
            "note": None,
        },
    }
    _assert_migration_digest_matches_runtime(
        PendingActionType.OWNER_STOCK_UPDATE,
        raw_payload,
    )


def test_phase_b_reordered_lines_have_same_migration_digest() -> None:
    first = {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_name": None,
            "customer_phone": "+919123456789",
            "pickup_at": "2026-08-01T10:00:00Z",
            "lines": [
                {"product_id": 1, "quantity": "1.00"},
                {"product_id": 2, "quantity": "2.00"},
            ],
            "customer_note": None,
        },
    }
    second = {
        **first,
        "data": {
            **first["data"],  # type: ignore[dict-item]
            "lines": list(reversed(first["data"]["lines"])),  # type: ignore[index]
        },
    }
    migration = _load_migration(MIGRATION_0002, "fonely_migration_0002_reorder")
    assert migration._digest("order", 1, first) == migration._digest("order", 1, second)


@pytest.mark.parametrize(
    ("action_type", "version", "payload"),
    [
        (
            "order",
            2,
            {
                "schema_version": 2,
                "action_type": "order",
                "data": {},
            },
        ),
        (
            "appointment",
            1,
            {
                "schema_version": 1,
                "action_type": "appointment",
                "data": {},
            },
        ),
        (
            "order",
            1,
            {
                "schema_version": 1,
                "action_type": "order",
                "data": {
                    "customer_name": None,
                    "customer_phone": "+919123456789",
                    "pickup_at": "2026-08-01T10:00:00Z",
                    "lines": [{"product_id": 1, "quantity": True}],
                    "customer_note": None,
                },
            },
        ),
        (
            "owner_stock_update",
            1,
            {
                "schema_version": 1,
                "action_type": "owner_stock_update",
                "data": {
                    "product_id": 1,
                    "business_date": "2026-08-01",
                    "operation": "set",
                    "quantity": 1.0,
                    "note": None,
                },
            },
        ),
    ],
)
def test_phase_b_migration_rejects_unsupported_or_invalid_payloads(
    action_type: str,
    version: int,
    payload: dict[str, object],
) -> None:
    migration = _load_migration(MIGRATION_0002, "fonely_migration_0002_invalid")
    with pytest.raises(RuntimeError):
        migration._digest(action_type, version, payload)


def test_0005_lock_set_is_deterministic_and_includes_fk_targets() -> None:
    migration = _load_migration(MIGRATION_0005, "fonely_migration_0005_lock_order")
    expected = tuple(
        sorted(
            (
                "calls",
                "inventory_balances",
                "inventory_movements",
                "inventory_operations",
                "inventory_reservations",
                "order_line_items",
                "orders",
                "pending_actions",
                "products",
            )
        )
    )
    assert expected == migration._AFFECTED_TABLES
    source = MIGRATION_0005.read_text()
    assert source.count("_lock_affected_tables()") == 3


def test_0005_inventory_order_tenant_composite_fk_signatures() -> None:
    captured = _capture_upgrade().metadata.tables
    balance_fks = _fk_signatures(captured["inventory_balances"])
    assert (
        ("business_id", "product_id"),
        ("products.business_id", "products.id"),
    ) in balance_fks

    reservation_fks = _fk_signatures(captured["inventory_reservations"])
    assert (
        ("business_id", "product_id"),
        ("products.business_id", "products.id"),
    ) in reservation_fks
    assert (
        ("business_id", "order_id"),
        ("orders.business_id", "orders.id"),
    ) in reservation_fks

    movement_fks = _fk_signatures(captured["inventory_movements"])
    assert (
        ("business_id", "product_id"),
        ("products.business_id", "products.id"),
    ) in movement_fks
    assert (
        ("business_id", "reservation_id"),
        ("inventory_reservations.business_id", "inventory_reservations.id"),
    ) in movement_fks

    order_fks = _fk_signatures(captured["orders"])
    assert (
        ("business_id", "pending_action_id"),
        ("pending_actions.business_id", "pending_actions.id"),
    ) in order_fks
    assert (
        ("business_id", "call_id"),
        ("calls.business_id", "calls.id"),
    ) in order_fks

    line_fks = _fk_signatures(captured["order_line_items"])
    assert (
        ("business_id", "order_id"),
        ("orders.business_id", "orders.id"),
    ) in line_fks
    assert (
        ("business_id", "product_id"),
        ("products.business_id", "products.id"),
    ) in line_fks


def test_0005_append_only_trigger_is_rendered() -> None:
    rendered = "\n".join(_capture_upgrade().executed_sql)
    assert "reject_inventory_movement_mutation" in rendered
    assert "ck_inventory_movement_append_only" in rendered
    assert "inventory movements are append-only" in rendered
    assert "BEFORE UPDATE OR DELETE ON inventory_movements" in rendered


def test_0005_order_line_immutability_trigger_is_rendered() -> None:
    rendered = "\n".join(_capture_upgrade().executed_sql)
    assert "reject_order_line_item_mutation" in rendered
    assert "ck_order_line_item_immutable" in rendered
    assert "order line items are immutable evidence" in rendered
    assert "BEFORE UPDATE OR DELETE ON order_line_items" in rendered


def test_0005_offline_guard_checks_order_line_items() -> None:
    source = MIGRATION_0005.read_text()
    offline_block = source[source.index("else:") : source.index("# --- Prerequisite")]
    assert "order_line_items" in offline_block
    assert "requires online backfill" in offline_block
