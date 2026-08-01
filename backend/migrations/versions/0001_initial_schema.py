"""initial_schema

Revision ID: 0001
Revises:
Create Date: 2026-07-31

Creates all 18 application tables in dependency order.
This is an immutable snapshot — do not import runtime models.
"""

from collections.abc import Sequence
from enum import StrEnum

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class SubscriptionStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Capability(StrEnum):
    INVENTORY = "inventory"
    APPOINTMENTS = "appointments"


class LocaleRole(StrEnum):
    OWNER = "owner"
    DEFAULT = "default"
    SUPPORTED = "supported"


class LanguageStatus(StrEnum):
    EXPERIMENTAL = "experimental"
    BETA = "beta"
    VERIFIED = "verified"


class BusinessUserRole(StrEnum):
    OWNER = "owner"
    MANAGER = "manager"


class ProductUnit(StrEnum):
    KG = "kg"
    GRAM = "gram"
    PIECE = "piece"
    DOZEN = "dozen"
    LITRE = "litre"
    ML = "ml"
    PACKET = "packet"


class PendingActionType(StrEnum):
    ORDER = "order"
    APPOINTMENT = "appointment"
    OWNER_STOCK_UPDATE = "owner_stock_update"
    OWNER_PRICE_UPDATE = "owner_price_update"
    OWNER_SCHEDULE_UPDATE = "owner_schedule_update"


class PendingActionStatus(StrEnum):
    COLLECTING_DETAILS = "collecting_details"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMMITTING = "committing"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CallerRole(StrEnum):
    OWNER = "owner"
    MANAGER = "manager"
    CUSTOMER = "customer"


class CallOutcome(StrEnum):
    ORDERED = "ordered"
    BOOKED = "booked"
    ENQUIRY = "enquiry"
    OUT_OF_STOCK = "out_of_stock"
    ESCALATED = "escalated"
    NO_ACTION = "no_action"
    DROPPED = "dropped"


class OrderStatus(StrEnum):
    CONFIRMED = "confirmed"
    PICKED_UP = "picked_up"
    CANCELLED = "cancelled"


class InventoryReservationStatus(StrEnum):
    ACTIVE = "active"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"


class InventoryMovementType(StrEnum):
    STOCK_ADDED = "stock_added"
    WALK_IN_SALE = "walk_in_sale"
    PHONE_ORDER_RESERVED = "phone_order_reserved"
    RESERVATION_RELEASED = "reservation_released"
    ORDER_COMPLETED = "order_completed"
    ORDER_CANCELLED = "order_cancelled"
    MANUAL_ADJUSTMENT = "manual_adjustment"


class AppointmentStatus(StrEnum):
    HELD = "held"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


def enum_type(enum_class: type[StrEnum], name: str) -> sa.Enum:
    return sa.Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


def upgrade() -> None:
    # 1. businesses
    op.create_table(
        "businesses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("owner_name", sa.String(200), nullable=True),
        sa.Column("primary_contact_phone", sa.String(20), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("location_lat", sa.Numeric(10, 7), nullable=True),
        sa.Column("location_lng", sa.Numeric(10, 7), nullable=True),
        sa.Column("timezone", sa.String(50), nullable=False),
        sa.Column("phone_number", sa.String(20), nullable=True),
        sa.Column(
            "subscription", enum_type(SubscriptionStatus, "subscription_status"), nullable=False
        ),
        sa.Column("paid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)",
            name="ck_businesses_lat_range",
        ),
        sa.CheckConstraint(
            "location_lng IS NULL OR (location_lng >= -180 AND location_lng <= 180)",
            name="ck_businesses_lng_range",
        ),
    )

    # 2. business_capabilities
    op.create_table(
        "business_capabilities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("capability", enum_type(Capability, "capability_type"), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "capability"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )

    # 3. business_locales
    op.create_table(
        "business_locales",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("locale_code", sa.String(10), nullable=False),
        sa.Column("role", enum_type(LocaleRole, "locale_role"), nullable=False),
        sa.Column("voice_speaker", sa.String(50), nullable=True),
        sa.Column("allow_code_switching", sa.Boolean(), nullable=False),
        sa.Column(
            "validation_status", enum_type(LanguageStatus, "language_status"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "locale_code", "role"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )

    # 4. business_users
    op.create_table(
        "business_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("role", enum_type(BusinessUserRole, "business_user_role"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "phone"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )

    # 5. operating_schedules
    op.create_table(
        "operating_schedules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("open_time", sa.Time(), nullable=False),
        sa.Column("close_time", sa.Time(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "day_of_week", "open_time"),
        sa.CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="ck_schedule_dow"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )

    # 6. schedule_exceptions
    op.create_table(
        "schedule_exceptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("exception_date", sa.Date(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False),
        sa.Column("open_time", sa.Time(), nullable=True),
        sa.Column("close_time", sa.Time(), nullable=True),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "exception_date"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )

    # 7. products
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("unit", enum_type(ProductUnit, "product_unit"), nullable=False),
        sa.Column("price_per_unit", sa.Numeric(10, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "name"),
        sa.CheckConstraint("price_per_unit >= 0", name="ck_product_price"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )

    # 8. services
    op.create_table(
        "services",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "name"),
        sa.CheckConstraint("duration_minutes > 0", name="ck_service_duration"),
        sa.CheckConstraint("price IS NULL OR price >= 0", name="ck_service_price"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )

    # 9. resources
    op.create_table(
        "resources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "name"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )

    # 10. pending_actions (needed before orders/appointments)
    op.create_table(
        "pending_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(100), nullable=True),
        sa.Column("action_type", enum_type(PendingActionType, "action_type"), nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False),
        sa.Column("proposed_payload", postgresql.JSONB(), nullable=False),
        sa.Column("confirmation_snapshot", sa.Text(), nullable=True),
        sa.Column(
            "status", enum_type(PendingActionStatus, "pending_action_status"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("initiated_by", sa.String(20), nullable=True),
        sa.Column("confirmed_by", sa.String(20), nullable=True),
        sa.Column("committed_entity_type", sa.String(50), nullable=True),
        sa.Column("committed_entity_id", sa.Integer(), nullable=True),
        sa.Column("commit_error_code", sa.String(50), nullable=True),
        sa.Column("commit_error_message", sa.String(500), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "idempotency_key", name="uq_pending_idempotency"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )
    op.create_index("ix_pending_actions_expiry", "pending_actions", ["status", "expires_at"])

    # 11. calls
    op.create_table(
        "calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("caller_phone", sa.String(20), nullable=True),
        sa.Column("caller_role", enum_type(CallerRole, "caller_role"), nullable=True),
        sa.Column("detected_language", sa.String(10), nullable=True),
        sa.Column("language_confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("outcome", enum_type(CallOutcome, "call_outcome"), nullable=True),
        sa.Column("transcript", postgresql.JSONB(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "language_confidence IS NULL OR "
            "(language_confidence >= 0 AND language_confidence <= 1)",
            name="ck_call_lang_confidence",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )

    # 12. inventory_balances
    op.create_table(
        "inventory_balances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("on_hand_qty", sa.Numeric(10, 2), nullable=False),
        sa.Column("reserved_qty", sa.Numeric(10, 2), nullable=False),
        sa.Column("available_tomorrow", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "product_id", "business_date"),
        sa.CheckConstraint("on_hand_qty >= 0", name="ck_inv_on_hand"),
        sa.CheckConstraint("reserved_qty >= 0", name="ck_inv_reserved"),
        sa.CheckConstraint("reserved_qty <= on_hand_qty", name="ck_inv_reserved_lte_on_hand"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
    )

    # 13. orders
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("customer_name", sa.String(200), nullable=True),
        sa.Column("customer_phone", sa.String(20), nullable=False),
        sa.Column("total_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("pickup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", enum_type(OrderStatus, "order_status"), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("pending_action_id", sa.Integer(), nullable=True),
        sa.Column("call_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "idempotency_key", name="uq_order_idempotency"),
        sa.CheckConstraint("total_amount >= 0", name="ck_order_total"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(["pending_action_id"], ["pending_actions.id"]),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"]),
    )
    op.create_index("ix_orders_business_status", "orders", ["business_id", "status"])

    # 14. order_line_items
    op.create_table(
        "order_line_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("product_name_snapshot", sa.String(200), nullable=False),
        sa.Column("qty", sa.Numeric(10, 2), nullable=False),
        sa.Column("unit", enum_type(ProductUnit, "line_item_unit"), nullable=False),
        sa.Column("price_per_unit_snapshot", sa.Numeric(10, 2), nullable=False),
        sa.Column("subtotal", sa.Numeric(10, 2), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("qty > 0", name="ck_line_qty"),
        sa.CheckConstraint("price_per_unit_snapshot >= 0", name="ck_line_price"),
        sa.CheckConstraint("subtotal >= 0", name="ck_line_subtotal"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
    )

    # 15. inventory_reservations
    op.create_table(
        "inventory_reservations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("pending_action_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("qty", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "status", enum_type(InventoryReservationStatus, "reservation_status"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_id",
            "idempotency_key",
            "product_id",
            name="uq_inv_res_idempotency",
        ),
        sa.CheckConstraint("qty > 0", name="ck_inv_res_qty"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["pending_action_id"], ["pending_actions.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
    )
    op.create_index(
        "ix_inv_res_active_expiry",
        "inventory_reservations",
        ["business_id", "status", "expires_at"],
    )

    # 16. inventory_movements
    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column(
            "movement_type",
            enum_type(InventoryMovementType, "movement_type"),
            nullable=False,
        ),
        sa.Column("on_hand_delta", sa.Numeric(10, 2), nullable=False),
        sa.Column("reserved_delta", sa.Numeric(10, 2), nullable=False),
        sa.Column("on_hand_after", sa.Numeric(10, 2), nullable=False),
        sa.Column("reserved_after", sa.Numeric(10, 2), nullable=False),
        sa.Column("available_after", sa.Numeric(10, 2), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("reservation_id", sa.Integer(), nullable=True),
        sa.Column("pending_action_id", sa.Integer(), nullable=True),
        sa.Column("initiated_by", sa.String(20), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["reservation_id"], ["inventory_reservations.id"]),
        sa.ForeignKeyConstraint(["pending_action_id"], ["pending_actions.id"]),
    )

    # 17. appointments
    op.create_table(
        "appointments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("customer_name", sa.String(200), nullable=True),
        sa.Column("customer_phone", sa.String(20), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", enum_type(AppointmentStatus, "appointment_status"), nullable=False),
        sa.Column("held_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("pending_action_id", sa.Integer(), nullable=True),
        sa.Column("call_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "idempotency_key", name="uq_appt_idempotency"),
        sa.CheckConstraint("end_at > start_at", name="ck_appt_time_order"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(["resource_id"], ["resources.id"]),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        sa.ForeignKeyConstraint(["pending_action_id"], ["pending_actions.id"]),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"]),
    )
    op.create_index(
        "ix_appointments_resource_lookup",
        "appointments",
        ["resource_id", "start_at", "end_at"],
    )

    # 18. owner_audit_log
    op.create_table(
        "owner_audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("initiated_by_phone", sa.String(20), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("pending_action_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(["pending_action_id"], ["pending_actions.id"]),
    )


def downgrade() -> None:
    op.drop_table("inventory_movements")
    op.drop_table("order_line_items")
    op.drop_index("ix_inv_res_active_expiry", table_name="inventory_reservations")
    op.drop_table("inventory_reservations")
    op.drop_table("owner_audit_log")
    op.drop_index("ix_orders_business_status", table_name="orders")
    op.drop_table("orders")
    op.drop_table("inventory_balances")
    op.drop_index("ix_appointments_resource_lookup", table_name="appointments")
    op.drop_table("appointments")
    op.drop_table("services")
    op.drop_table("schedule_exceptions")
    op.drop_table("resources")
    op.drop_table("products")
    op.drop_index("ix_pending_actions_expiry", table_name="pending_actions")
    op.drop_table("pending_actions")
    op.drop_table("operating_schedules")
    op.drop_table("calls")
    op.drop_table("business_users")
    op.drop_table("business_locales")
    op.drop_table("business_capabilities")
    op.drop_table("businesses")
