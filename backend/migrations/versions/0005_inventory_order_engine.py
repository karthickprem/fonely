"""inventory_order_engine

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-02

Hardens populated inventory/order tables with tenant-composite foreign keys,
durable direct-inventory idempotency, append-only movement enforcement,
movement coherence checks, and balance version constraints. Adds the
inventory_operations table for database-backed idempotency records.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fail_if_exists(sql: str, message: str) -> None:
    if op.get_bind().execute(sa.text(sql)).scalar_one_or_none() is not None:
        raise RuntimeError(message)


_AFFECTED_TABLES = (
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


def _lock_affected_tables() -> None:
    table_names = ", ".join(f"'{t}'" for t in _AFFECTED_TABLES)
    op.execute(
        f"""DO $migration_lock$
        DECLARE
            table_name text;
        BEGIN
            FOREACH table_name IN ARRAY ARRAY[{table_names}] LOOP
                IF to_regclass(format('%I.%I', current_schema(), table_name)) IS NOT NULL THEN
                    EXECUTE format(
                        'LOCK TABLE %I.%I IN SHARE ROW EXCLUSIVE MODE',
                        current_schema(), table_name
                    );
                END IF;
            END LOOP;
        END
        $migration_lock$"""
    )


def _preflight_upgrade() -> None:
    _fail_if_exists(
        "SELECT 1 FROM inventory_balances WHERE version < 1 LIMIT 1",
        "Migration 0005 requires all inventory balances to have version >= 1",
    )
    _fail_if_exists(
        "SELECT 1 FROM inventory_movements "
        "WHERE available_after IS DISTINCT FROM on_hand_after - reserved_after LIMIT 1",
        "Migration 0005 requires movement coherence (available = on_hand - reserved)",
    )
    _fail_if_exists(
        "SELECT 1 FROM inventory_balances ib "
        "LEFT JOIN products p ON p.id = ib.product_id "
        "WHERE p.id IS NULL OR p.business_id IS DISTINCT FROM ib.business_id LIMIT 1",
        "Migration 0005 found cross-tenant product reference in inventory balances",
    )
    _fail_if_exists(
        "SELECT 1 FROM inventory_reservations ir "
        "LEFT JOIN products p ON p.id = ir.product_id "
        "WHERE p.id IS NULL OR p.business_id IS DISTINCT FROM ir.business_id LIMIT 1",
        "Migration 0005 found cross-tenant product reference in inventory reservations",
    )
    _fail_if_exists(
        "SELECT 1 FROM inventory_movements im "
        "LEFT JOIN products p ON p.id = im.product_id "
        "WHERE p.id IS NULL OR p.business_id IS DISTINCT FROM im.business_id LIMIT 1",
        "Migration 0005 found cross-tenant product reference in inventory movements",
    )
    _fail_if_exists(
        "SELECT 1 FROM inventory_reservations ir "
        "LEFT JOIN orders o ON o.id = ir.order_id "
        "WHERE ir.order_id IS NOT NULL "
        "AND (o.id IS NULL OR o.business_id IS DISTINCT FROM ir.business_id) LIMIT 1",
        "Migration 0005 found cross-tenant order reference in inventory reservations",
    )
    _fail_if_exists(
        "SELECT 1 FROM inventory_reservations ir "
        "LEFT JOIN pending_actions p ON p.id = ir.pending_action_id "
        "WHERE ir.pending_action_id IS NOT NULL "
        "AND (p.id IS NULL OR p.business_id IS DISTINCT FROM ir.business_id) LIMIT 1",
        "Migration 0005 found cross-tenant pending action reference in inventory reservations",
    )
    _fail_if_exists(
        "SELECT 1 FROM inventory_movements im "
        "LEFT JOIN orders o ON o.id = im.order_id "
        "WHERE im.order_id IS NOT NULL "
        "AND (o.id IS NULL OR o.business_id IS DISTINCT FROM im.business_id) LIMIT 1",
        "Migration 0005 found cross-tenant order reference in inventory movements",
    )
    _fail_if_exists(
        "SELECT 1 FROM inventory_movements im "
        "LEFT JOIN pending_actions p ON p.id = im.pending_action_id "
        "WHERE im.pending_action_id IS NOT NULL "
        "AND (p.id IS NULL OR p.business_id IS DISTINCT FROM im.business_id) LIMIT 1",
        "Migration 0005 found cross-tenant pending action reference in inventory movements",
    )
    _fail_if_exists(
        "SELECT 1 FROM inventory_movements im "
        "LEFT JOIN inventory_reservations ir ON ir.id = im.reservation_id "
        "WHERE im.reservation_id IS NOT NULL "
        "AND (ir.id IS NULL OR ir.business_id IS DISTINCT FROM im.business_id) LIMIT 1",
        "Migration 0005 found cross-tenant reservation reference in inventory movements",
    )
    _fail_if_exists(
        "SELECT 1 FROM orders o "
        "LEFT JOIN pending_actions p ON p.id = o.pending_action_id "
        "WHERE o.pending_action_id IS NOT NULL "
        "AND (p.id IS NULL OR p.business_id IS DISTINCT FROM o.business_id) LIMIT 1",
        "Migration 0005 found cross-tenant pending action reference in orders",
    )
    _fail_if_exists(
        "SELECT 1 FROM orders o "
        "LEFT JOIN calls c ON c.id = o.call_id "
        "WHERE o.call_id IS NOT NULL "
        "AND (c.id IS NULL OR c.business_id IS DISTINCT FROM o.business_id) LIMIT 1",
        "Migration 0005 found cross-tenant call reference in orders",
    )
    _fail_if_exists(
        "SELECT 1 FROM order_line_items li1 "
        "JOIN order_line_items li2 ON li1.order_id = li2.order_id "
        "AND li1.product_id = li2.product_id AND li1.id < li2.id LIMIT 1",
        "Migration 0005 found duplicate order line items per product",
    )
    _fail_if_exists(
        "SELECT 1 FROM order_line_items li "
        "JOIN orders o ON o.id = li.order_id "
        "LEFT JOIN products p ON p.id = li.product_id "
        "WHERE p.id IS NULL OR p.business_id IS DISTINCT FROM o.business_id LIMIT 1",
        "Migration 0005 found cross-tenant product reference in order line items",
    )


def _preflight_downgrade() -> None:
    _fail_if_exists(
        "SELECT 1 FROM inventory_operations LIMIT 1",
        "Migration 0005 downgrade cannot discard inventory operation records",
    )
    _fail_if_exists(
        "SELECT 1 FROM order_line_items li "
        "JOIN orders o ON o.id = li.order_id "
        "WHERE li.business_id IS DISTINCT FROM o.business_id LIMIT 1",
        "Migration 0005 downgrade found inconsistent order line item business_id",
    )


def _backfill_line_item_business_id() -> None:
    op.execute(
        sa.text(
            "UPDATE order_line_items li SET business_id = o.business_id "
            "FROM orders o WHERE o.id = li.order_id AND li.business_id IS NULL"
        )
    )


def upgrade() -> None:
    _lock_affected_tables()

    if not context.is_offline_mode():
        _preflight_upgrade()
    else:
        op.execute(
            sa.text(
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM order_line_items LIMIT 1) "
                "THEN CAST('Migration 0005 requires online backfill "
                "for order_line_items.business_id' AS integer) END"
            )
        )

    # --- Prerequisite unique constraints for tenant-composite FKs ---
    op.create_unique_constraint("uq_products_business_id_id", "products", ["business_id", "id"])
    op.create_unique_constraint("uq_orders_business_id_id", "orders", ["business_id", "id"])
    op.create_unique_constraint(
        "uq_inv_reservations_business_id_id", "inventory_reservations", ["business_id", "id"]
    )
    op.create_unique_constraint(
        "uq_inv_movements_business_id_id", "inventory_movements", ["business_id", "id"]
    )

    # --- InventoryBalance hardening ---
    op.create_check_constraint("ck_inv_balance_version", "inventory_balances", "version > 0")
    op.drop_constraint(
        "inventory_balances_product_id_fkey", "inventory_balances", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_inv_balance_business_product",
        "inventory_balances",
        "products",
        ["business_id", "product_id"],
        ["business_id", "id"],
    )

    # --- InventoryReservation tenant-composite FKs ---
    op.drop_constraint(
        "inventory_reservations_product_id_fkey", "inventory_reservations", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_inv_res_business_product",
        "inventory_reservations",
        "products",
        ["business_id", "product_id"],
        ["business_id", "id"],
    )
    op.drop_constraint(
        "inventory_reservations_order_id_fkey", "inventory_reservations", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_inv_res_business_order",
        "inventory_reservations",
        "orders",
        ["business_id", "order_id"],
        ["business_id", "id"],
    )
    op.drop_constraint(
        "inventory_reservations_pending_action_id_fkey",
        "inventory_reservations",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_inv_res_business_pending_action",
        "inventory_reservations",
        "pending_actions",
        ["business_id", "pending_action_id"],
        ["business_id", "id"],
    )

    # --- InventoryMovement tenant-composite FKs ---
    op.drop_constraint(
        "inventory_movements_product_id_fkey", "inventory_movements", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_inv_mov_business_product",
        "inventory_movements",
        "products",
        ["business_id", "product_id"],
        ["business_id", "id"],
    )
    op.drop_constraint(
        "inventory_movements_order_id_fkey", "inventory_movements", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_inv_mov_business_order",
        "inventory_movements",
        "orders",
        ["business_id", "order_id"],
        ["business_id", "id"],
    )
    op.drop_constraint(
        "inventory_movements_reservation_id_fkey", "inventory_movements", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_inv_mov_business_reservation",
        "inventory_movements",
        "inventory_reservations",
        ["business_id", "reservation_id"],
        ["business_id", "id"],
    )
    op.drop_constraint(
        "inventory_movements_pending_action_id_fkey", "inventory_movements", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_inv_mov_business_pending_action",
        "inventory_movements",
        "pending_actions",
        ["business_id", "pending_action_id"],
        ["business_id", "id"],
    )

    # --- Order tenant-composite FKs ---
    op.drop_constraint("orders_pending_action_id_fkey", "orders", type_="foreignkey")
    op.create_foreign_key(
        "fk_order_business_pending_action",
        "orders",
        "pending_actions",
        ["business_id", "pending_action_id"],
        ["business_id", "id"],
    )
    op.drop_constraint("orders_call_id_fkey", "orders", type_="foreignkey")
    op.create_foreign_key(
        "fk_order_business_call",
        "orders",
        "calls",
        ["business_id", "call_id"],
        ["business_id", "id"],
    )

    # --- OrderLineItem hardening ---
    op.create_unique_constraint(
        "uq_line_item_order_product", "order_line_items", ["order_id", "product_id"]
    )
    op.add_column("order_line_items", sa.Column("business_id", sa.Integer(), nullable=True))
    if not context.is_offline_mode():
        _backfill_line_item_business_id()
    op.alter_column("order_line_items", "business_id", nullable=False)
    op.drop_constraint("order_line_items_order_id_fkey", "order_line_items", type_="foreignkey")
    op.drop_constraint("order_line_items_product_id_fkey", "order_line_items", type_="foreignkey")
    op.create_foreign_key(
        "fk_line_item_business_order",
        "order_line_items",
        "orders",
        ["business_id", "order_id"],
        ["business_id", "id"],
    )
    op.create_foreign_key(
        "fk_line_item_business_product",
        "order_line_items",
        "products",
        ["business_id", "product_id"],
        ["business_id", "id"],
    )

    # --- Movement coherence check ---
    op.create_check_constraint(
        "ck_inv_mov_available_coherence",
        "inventory_movements",
        "available_after = on_hand_after - reserved_after",
    )

    # --- Durable idempotency table ---
    op.create_table(
        "inventory_operations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("movement_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "idempotency_key", name="uq_inv_op_idempotency"),
        sa.CheckConstraint(
            "operation IN ('set', 'add', 'walk_in')",
            name="ck_inv_op_operation",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(
            ["business_id", "product_id"],
            ["products.business_id", "products.id"],
            name="fk_inv_op_business_product",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "movement_id"],
            ["inventory_movements.business_id", "inventory_movements.id"],
            name="fk_inv_op_business_movement",
        ),
    )

    # --- Append-only trigger on inventory_movements ---
    op.execute(
        sa.text(
            """CREATE FUNCTION reject_inventory_movement_mutation()
               RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_inventory_movement_append_only',
                       MESSAGE = 'inventory movements are append-only';
               END;
               $$"""
        )
    )
    op.execute(
        sa.text(
            """CREATE TRIGGER ck_inventory_movement_append_only
               BEFORE UPDATE OR DELETE ON inventory_movements
               FOR EACH ROW EXECUTE FUNCTION reject_inventory_movement_mutation()"""
        )
    )


def downgrade() -> None:
    _lock_affected_tables()

    if not context.is_offline_mode():
        _preflight_downgrade()
    else:
        op.execute(
            sa.text(
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM inventory_operations LIMIT 1) "
                "THEN CAST('Migration 0005 downgrade requires online representability preflight' "
                "AS integer) END"
            )
        )

    # --- Drop append-only trigger ---
    op.execute(sa.text("DROP TRIGGER ck_inventory_movement_append_only ON inventory_movements"))
    op.execute(sa.text("DROP FUNCTION reject_inventory_movement_mutation()"))

    # --- Drop durable idempotency table ---
    op.drop_table("inventory_operations")

    # --- Drop movement coherence check ---
    op.drop_constraint("ck_inv_mov_available_coherence", "inventory_movements", type_="check")

    # --- Restore OrderLineItem ---
    op.drop_constraint("fk_line_item_business_product", "order_line_items", type_="foreignkey")
    op.drop_constraint("fk_line_item_business_order", "order_line_items", type_="foreignkey")
    op.create_foreign_key(
        "order_line_items_order_id_fkey",
        "order_line_items",
        "orders",
        ["order_id"],
        ["id"],
    )
    op.create_foreign_key(
        "order_line_items_product_id_fkey",
        "order_line_items",
        "products",
        ["product_id"],
        ["id"],
    )
    op.drop_column("order_line_items", "business_id")
    op.drop_constraint("uq_line_item_order_product", "order_line_items", type_="unique")

    # --- Restore Order FKs ---
    op.drop_constraint("fk_order_business_call", "orders", type_="foreignkey")
    op.create_foreign_key("orders_call_id_fkey", "orders", "calls", ["call_id"], ["id"])
    op.drop_constraint("fk_order_business_pending_action", "orders", type_="foreignkey")
    op.create_foreign_key(
        "orders_pending_action_id_fkey",
        "orders",
        "pending_actions",
        ["pending_action_id"],
        ["id"],
    )

    # --- Restore InventoryMovement FKs ---
    op.drop_constraint(
        "fk_inv_mov_business_pending_action", "inventory_movements", type_="foreignkey"
    )
    op.create_foreign_key(
        "inventory_movements_pending_action_id_fkey",
        "inventory_movements",
        "pending_actions",
        ["pending_action_id"],
        ["id"],
    )
    op.drop_constraint("fk_inv_mov_business_reservation", "inventory_movements", type_="foreignkey")
    op.create_foreign_key(
        "inventory_movements_reservation_id_fkey",
        "inventory_movements",
        "inventory_reservations",
        ["reservation_id"],
        ["id"],
    )
    op.drop_constraint("fk_inv_mov_business_order", "inventory_movements", type_="foreignkey")
    op.create_foreign_key(
        "inventory_movements_order_id_fkey",
        "inventory_movements",
        "orders",
        ["order_id"],
        ["id"],
    )
    op.drop_constraint("fk_inv_mov_business_product", "inventory_movements", type_="foreignkey")
    op.create_foreign_key(
        "inventory_movements_product_id_fkey",
        "inventory_movements",
        "products",
        ["product_id"],
        ["id"],
    )

    # --- Restore InventoryReservation FKs ---
    op.drop_constraint(
        "fk_inv_res_business_pending_action", "inventory_reservations", type_="foreignkey"
    )
    op.create_foreign_key(
        "inventory_reservations_pending_action_id_fkey",
        "inventory_reservations",
        "pending_actions",
        ["pending_action_id"],
        ["id"],
    )
    op.drop_constraint("fk_inv_res_business_order", "inventory_reservations", type_="foreignkey")
    op.create_foreign_key(
        "inventory_reservations_order_id_fkey",
        "inventory_reservations",
        "orders",
        ["order_id"],
        ["id"],
    )
    op.drop_constraint("fk_inv_res_business_product", "inventory_reservations", type_="foreignkey")
    op.create_foreign_key(
        "inventory_reservations_product_id_fkey",
        "inventory_reservations",
        "products",
        ["product_id"],
        ["id"],
    )

    # --- Restore InventoryBalance ---
    op.drop_constraint("fk_inv_balance_business_product", "inventory_balances", type_="foreignkey")
    op.create_foreign_key(
        "inventory_balances_product_id_fkey",
        "inventory_balances",
        "products",
        ["product_id"],
        ["id"],
    )
    op.drop_constraint("ck_inv_balance_version", "inventory_balances", type_="check")

    # --- Drop prerequisite UQs (reverse order) ---
    op.drop_constraint("uq_inv_movements_business_id_id", "inventory_movements", type_="unique")
    op.drop_constraint(
        "uq_inv_reservations_business_id_id", "inventory_reservations", type_="unique"
    )
    op.drop_constraint("uq_orders_business_id_id", "orders", type_="unique")
    op.drop_constraint("uq_products_business_id_id", "products", type_="unique")
