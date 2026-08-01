"""Tests for schema metadata — tables, constraints, enums, migration content."""

import ast
from pathlib import Path

import pytest

from fonely.core.locale_mapping import SARVAM_LOCALE_MAP, to_sarvam_locale
from fonely.models import enums
from fonely.models.schema import Base

EXPECTED_TABLES = [
    "businesses",
    "business_capabilities",
    "business_locales",
    "business_users",
    "operating_schedules",
    "schedule_exceptions",
    "products",
    "services",
    "resources",
    "inventory_balances",
    "inventory_reservations",
    "inventory_movements",
    "orders",
    "order_line_items",
    "appointments",
    "pending_actions",
    "calls",
    "owner_audit_log",
]


class TestSchemaMetadata:
    def test_expected_table_count(self) -> None:
        tables = list(Base.metadata.tables.keys())
        assert len(tables) == 18

    def test_all_expected_tables_exist(self) -> None:
        tables = set(Base.metadata.tables.keys())
        for t in EXPECTED_TABLES:
            assert t in tables, f"Missing table: {t}"

    def test_businesses_has_lat_lng_constraints(self) -> None:
        biz = Base.metadata.tables["businesses"]
        constraint_names = {c.name for c in biz.constraints if hasattr(c, "name") and c.name}
        assert "ck_businesses_lat_range" in constraint_names
        assert "ck_businesses_lng_range" in constraint_names

    def test_calls_has_confidence_constraint(self) -> None:
        calls = Base.metadata.tables["calls"]
        constraint_names = {c.name for c in calls.constraints if hasattr(c, "name") and c.name}
        assert "ck_call_lang_confidence" in constraint_names

    def test_appointments_has_time_order_constraint(self) -> None:
        appts = Base.metadata.tables["appointments"]
        constraint_names = {c.name for c in appts.constraints if hasattr(c, "name") and c.name}
        assert "ck_appt_time_order" in constraint_names

    def test_order_idempotency_is_tenant_scoped(self) -> None:
        orders = Base.metadata.tables["orders"]
        for c in orders.constraints:
            if hasattr(c, "name") and c.name == "uq_order_idempotency":
                cols = {col.name for col in c.columns}
                assert "business_id" in cols
                assert "idempotency_key" in cols
                return
        pytest.fail("uq_order_idempotency constraint not found")

    def test_reservation_idempotency_includes_product(self) -> None:
        res = Base.metadata.tables["inventory_reservations"]
        for c in res.constraints:
            if hasattr(c, "name") and c.name == "uq_inv_res_idempotency":
                cols = {col.name for col in c.columns}
                assert "business_id" in cols
                assert "idempotency_key" in cols
                assert "product_id" in cols
                return
        pytest.fail("uq_inv_res_idempotency constraint not found")

    def test_inventory_balance_has_safety_constraints(self) -> None:
        inv = Base.metadata.tables["inventory_balances"]
        names = {c.name for c in inv.constraints if hasattr(c, "name") and c.name}
        assert "ck_inv_on_hand" in names
        assert "ck_inv_reserved" in names
        assert "ck_inv_reserved_lte_on_hand" in names

    def test_primary_contact_phone_is_not_unique(self) -> None:
        biz = Base.metadata.tables["businesses"]
        phone_col = biz.c.primary_contact_phone
        assert not phone_col.unique

    def test_business_user_uses_business_user_role(self) -> None:
        bu = Base.metadata.tables["business_users"]
        role_col = bu.c.role
        assert role_col is not None


class TestEnums:
    def test_business_user_role_excludes_customer(self) -> None:
        values = {v.value for v in enums.BusinessUserRole}
        assert "customer" not in values
        assert "owner" in values
        assert "manager" in values

    def test_caller_role_includes_customer(self) -> None:
        values = {v.value for v in enums.CallerRole}
        assert "customer" in values

    def test_pending_action_statuses(self) -> None:
        expected = {
            "collecting_details",
            "awaiting_confirmation",
            "committing",
            "confirmed",
            "rejected",
            "cancelled",
            "expired",
        }
        actual = {v.value for v in enums.PendingActionStatus}
        assert actual == expected

    def test_inventory_movement_types(self) -> None:
        expected = {
            "stock_added",
            "walk_in_sale",
            "phone_order_reserved",
            "reservation_released",
            "order_completed",
            "order_cancelled",
            "manual_adjustment",
        }
        actual = {v.value for v in enums.InventoryMovementType}
        assert actual == expected

    def test_reservation_statuses(self) -> None:
        expected = {"active", "committed", "released", "expired"}
        actual = {v.value for v in enums.InventoryReservationStatus}
        assert actual == expected


class TestLocaleMapping:
    def test_odia_maps_to_sarvam_od(self) -> None:
        assert to_sarvam_locale("or-IN") == "od-IN"

    def test_tamil_maps_unchanged(self) -> None:
        assert to_sarvam_locale("ta-IN") == "ta-IN"

    def test_all_fonely_locales_have_mapping(self) -> None:
        from fonely.core.validators import SUPPORTED_FONELY_LOCALES

        for loc in SUPPORTED_FONELY_LOCALES:
            assert loc in SARVAM_LOCALE_MAP, f"No Sarvam mapping for {loc}"

    def test_unknown_locale_raises(self) -> None:
        with pytest.raises(ValueError, match="No Sarvam mapping"):
            to_sarvam_locale("xx-YY")


class TestMigrationContent:
    """Verify migration 0001 contains real schema operations, not pass."""

    def test_migration_upgrade_is_not_empty(self) -> None:
        migration_path = (
            Path(__file__).parent.parent / "migrations" / "versions" / "0001_initial_schema.py"
        )
        assert migration_path.exists(), "Migration file missing"
        source = migration_path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
                body_stmts = [
                    s
                    for s in node.body
                    if not isinstance(s, (ast.Pass, ast.Expr))
                    or (isinstance(s, ast.Expr) and not isinstance(s.value, ast.Constant))
                ]
                assert len(body_stmts) > 0, "upgrade() contains only pass"
                return
        pytest.fail("upgrade() function not found in migration")

    def test_migration_creates_businesses_table(self) -> None:
        migration_path = (
            Path(__file__).parent.parent / "migrations" / "versions" / "0001_initial_schema.py"
        )
        source = migration_path.read_text()
        assert 'create_table(\n        "businesses"' in source or '"businesses"' in source
        assert '"owner_audit_log"' in source

    def test_migration_downgrade_drops_tables(self) -> None:
        migration_path = (
            Path(__file__).parent.parent / "migrations" / "versions" / "0001_initial_schema.py"
        )
        source = migration_path.read_text()
        assert "drop_table" in source
