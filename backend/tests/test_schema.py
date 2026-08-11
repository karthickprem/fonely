"""Tests for schema metadata — tables, constraints, enums, migration content."""

import ast
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

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
    "service_resource_eligibility",
    "inventory_balances",
    "inventory_reservations",
    "inventory_movements",
    "inventory_operations",
    "orders",
    "order_line_items",
    "appointments",
    "resource_allocations",
    "appointment_commits",
    "pending_actions",
    "calls",
    "owner_audit_log",
    "business_onboarding_drafts",
    "business_configuration_commits",
    "notification_outbox",
    "conversations",
    "conversation_turns",
    "business_daily_context",
    "whatsapp_inbound_events",
    "whatsapp_delivery_attempts",
    "notification_manifests",
]


class TestSchemaMetadata:
    def test_expected_table_count(self) -> None:
        tables = list(Base.metadata.tables.keys())
        assert len(tables) == 31

    def test_all_expected_tables_exist(self) -> None:
        tables = set(Base.metadata.tables.keys())
        for t in EXPECTED_TABLES:
            assert t in tables, f"Missing table: {t}"

    def test_businesses_has_lat_lng_constraints(self) -> None:
        biz = Base.metadata.tables["businesses"]
        constraint_names = {c.name for c in biz.constraints if hasattr(c, "name") and c.name}
        assert "ck_businesses_lat_range" in constraint_names
        assert "ck_businesses_lng_range" in constraint_names

    def test_businesses_has_bounded_appointment_policy_defaults(self) -> None:
        businesses = Base.metadata.tables["businesses"]
        checks = {constraint.name for constraint in businesses.constraints}
        assert {
            "ck_businesses_appointment_horizon",
            "ck_businesses_appointment_notice",
            "ck_businesses_appointment_slot_interval",
        } <= checks
        assert str(businesses.c.appointment_booking_horizon_days.server_default.arg) == "90"
        assert str(businesses.c.appointment_minimum_notice_minutes.server_default.arg) == "0"
        assert str(businesses.c.appointment_slot_interval_minutes.server_default.arg) == "15"

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

    def test_resource_allocation_is_capacity_authority(self) -> None:
        allocations = Base.metadata.tables["resource_allocations"]
        constraint_names = {constraint.name for constraint in allocations.constraints}
        assert "ex_resource_allocations_active_overlap" in constraint_names
        indexes = {index.name: index for index in allocations.indexes}
        active = indexes["uq_allocation_active_appointment"]
        assert active.unique
        predicate = str(active.dialect_options["postgresql"]["where"])
        assert predicate == "appointment_id IS NOT NULL AND status = 'active'"
        appointment_facts = next(
            constraint
            for constraint in allocations.foreign_key_constraints
            if constraint.name == "fk_allocation_business_appointment"
        )
        assert appointment_facts.deferrable is True
        assert appointment_facts.initially == "DEFERRED"

    def test_allocation_type_source_link_semantics_are_constrained(self) -> None:
        allocations = Base.metadata.tables["resource_allocations"]
        constraint = next(
            item
            for item in allocations.constraints
            if item.name == "ck_allocation_type_source_link"
        )
        sql = " ".join(str(constraint.sqltext).split())
        for combination in (
            "allocation_type = 'appointment' AND appointment_id IS NOT NULL "
            "AND pending_action_id IS NOT NULL AND source = 'customer_conversation'",
            "allocation_type = 'manual_appointment' AND appointment_id IS NOT NULL "
            "AND pending_action_id IS NULL AND source = 'owner_manual'",
            "allocation_type = 'walk_in' AND appointment_id IS NOT NULL "
            "AND pending_action_id IS NULL AND source = 'walk_in'",
            "allocation_type = 'owner_block' AND appointment_id IS NULL "
            "AND pending_action_id IS NULL AND source = 'owner_block'",
        ):
            assert combination in sql
        assert set(allocations.c.source.type.enums) == {
            "customer_conversation",
            "owner_manual",
            "walk_in",
            "owner_block",
        }

    def test_appointment_history_is_not_coupled_to_current_eligibility(self) -> None:
        appointments = Base.metadata.tables["appointments"]
        targets = {
            element.target_fullname
            for constraint in appointments.foreign_key_constraints
            for element in constraint.elements
        }
        assert all(not target.startswith("service_resource_eligibility.") for target in targets)

    def test_appointment_status_excludes_holds_and_has_no_default(self) -> None:
        appointments = Base.metadata.tables["appointments"]
        status = appointments.c.status
        assert set(status.type.enums) == {"confirmed", "completed", "cancelled", "no_show"}
        assert status.default is None
        assert status.server_default is None
        assert "held_until" not in appointments.c

    def test_appointment_source_uses_validating_enum_and_one_explicit_check(self) -> None:
        appointments = Base.metadata.tables["appointments"]
        source = appointments.c.source
        assert set(source.type.enums) == {"customer_conversation", "owner_manual", "walk_in"}
        assert source.type.name == "appointment_source"
        assert source.type.validate_strings is True
        assert source.type.create_constraint is False
        assert (
            sum(
                constraint.name == "ck_appointment_source"
                for constraint in appointments.constraints
            )
            == 1
        )
        processor = source.type.bind_processor(postgresql.dialect())
        assert processor is not None
        with pytest.raises(LookupError):
            processor("invalid_source")

    def test_resource_allocation_status_has_database_default(self) -> None:
        status = Base.metadata.tables["resource_allocations"].c.status
        assert status.default is not None
        assert status.server_default is not None
        assert str(status.server_default.arg) == "active"

    def test_appointment_commit_supports_mutations_only(self) -> None:
        commits = Base.metadata.tables["appointment_commits"]
        operation = commits.c.operation.type
        assert set(operation.enums) == {"cancel", "reschedule"}

    def test_schedule_scope_indexes_are_partial(self) -> None:
        schedules = Base.metadata.tables["operating_schedules"]
        indexes = {index.name: index for index in schedules.indexes}
        assert str(
            indexes["uq_schedule_business_scope"].dialect_options["postgresql"]["where"]
        ) == ("resource_id IS NULL")
        assert str(
            indexes["uq_schedule_resource_scope"].dialect_options["postgresql"]["where"]
        ) == ("resource_id IS NOT NULL")


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
