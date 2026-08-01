"""Structural parity checks between ORM metadata and migration 0001.

The migration is executed against a recording Alembic-op adapter. The captured
SQLAlchemy tables are compared to ORM metadata for table/column/type/nullability,
PKs, FKs, uniques, checks, and indexes. This is stronger than source-text tests,
while a live PostgreSQL ``alembic check`` remains the final integration proof.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

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


class OperationRecorder:
    """Minimal Alembic Operations-compatible recorder for migration 0001."""

    def __init__(self) -> None:
        self.metadata = sa.MetaData()
        self.indexes: dict[str, set[tuple[str, tuple[str, ...], bool]]] = {}
        self.dropped_tables: list[str] = []
        self.executed_sql: list[str] = []

    def execute(self, statement: str) -> None:
        self.executed_sql.append(statement)

    def create_table(self, name: str, *elements: Any, **_: Any) -> sa.Table:
        return sa.Table(name, self.metadata, *elements)

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        unique: bool = False,
        **_: Any,
    ) -> None:
        self.indexes.setdefault(table_name, set()).add((name, tuple(columns), unique))

    def drop_index(self, name: str, table_name: str, **_: Any) -> None:
        self.indexes.setdefault(table_name, set()).discard((name, (), False))

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
        constraint = next(item for item in table.constraints if item.name == name)
        table.constraints.remove(constraint)

    def drop_table(self, name: str, **_: Any) -> None:
        self.dropped_tables.append(name)

    def add_column(self, table_name: str, column: sa.Column[Any]) -> None:
        self.metadata.tables[table_name].append_column(column)

    def alter_column(self, table_name: str, column_name: str, **changes: Any) -> None:
        column = self.metadata.tables[table_name].c[column_name]
        if "nullable" in changes:
            column.nullable = changes["nullable"]

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
    ):
        module = _load_migration(path, name)
        module.op = recorder
        if name == "fonely_migration_0002":
            module.context = SimpleNamespace(is_offline_mode=lambda: True)
        module.upgrade()
    return recorder


def _capture_downgrade() -> OperationRecorder:
    recorder = _capture_upgrade()
    recorder.dropped_tables.clear()
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
    return {
        (
            tuple(element.parent.name for element in constraint.elements),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.foreign_key_constraints
    }


def _unique_signatures(table: sa.Table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }


def _check_signatures(table: sa.Table) -> set[tuple[str | None, str]]:
    return {
        (constraint.name, " ".join(str(constraint.sqltext).split()))
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


def _orm_index_signatures(table: sa.Table) -> set[tuple[str, tuple[str, ...], bool]]:
    return {
        (
            index.name or "",
            tuple(expression.name for expression in index.expressions),
            index.unique,
        )
        for index in table.indexes
    }


def test_migration_and_orm_have_identical_application_tables() -> None:
    captured = _capture_upgrade()
    assert set(captured.metadata.tables) == set(Base.metadata.tables)
    assert len(captured.metadata.tables) == 18


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


def test_migration_and_orm_constraint_parity() -> None:
    captured = _capture_upgrade()
    for name, orm_table in Base.metadata.tables.items():
        migration_table = captured.metadata.tables[name]
        assert _fk_signatures(migration_table) == _fk_signatures(orm_table), name
        assert _unique_signatures(migration_table) == _unique_signatures(orm_table), name
        assert _check_signatures(migration_table) == _check_signatures(orm_table), name


def test_migration_and_orm_index_parity() -> None:
    captured = _capture_upgrade()
    for name, orm_table in Base.metadata.tables.items():
        assert captured.indexes.get(name, set()) == _orm_index_signatures(orm_table), name


def test_migration_downgrade_drops_all_tables_in_reverse_dependency_order() -> None:
    recorder = _capture_downgrade()
    expected = [table.name for table in reversed(Base.metadata.sorted_tables)]
    assert recorder.dropped_tables == expected


def test_initial_migration_does_not_create_postgresql_extensions() -> None:
    recorder = _capture_upgrade()
    assert all("CREATE EXTENSION" not in statement for statement in recorder.executed_sql)


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
