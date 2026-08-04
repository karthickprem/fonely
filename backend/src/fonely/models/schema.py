"""Database models — capability-based schema.

Guarantees status:
- Phone/locale/timezone: validated at the Pydantic command layer, not ORM.
- Optimistic locking: version columns enforced via explicit conditional
  UPDATE in domain services, not via version_id_col mapper config.
- Appointment overlap: ix_appointments_resource_lookup is a query index.
  PostgreSQL exclusion constraint will be added in the appointment phase.
- JSONB payloads: typed as Any in ORM; every write/read goes through
  versioned Pydantic models in domain services.
- Authorization: BusinessUser is the sole authority. primary_contact_phone
  is onboarding metadata only.
"""

import uuid as _uuid
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum as PythonEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    literal_column,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from fonely.models.enums import (
    AppointmentCommitOperation,
    AppointmentSource,
    AppointmentStatus,
    BusinessUserRole,
    CallerRole,
    CallOutcome,
    Capability,
    DailyContextType,
    InventoryMovementType,
    InventoryReservationStatus,
    LanguageStatus,
    LocaleRole,
    NotificationChannel,
    NotificationEventType,
    NotificationRecipientType,
    NotificationStatus,
    OnboardingDraftStatus,
    OrderStatus,
    PendingActionStatus,
    PendingActionType,
    ProductUnit,
    ResourceAllocationSource,
    ResourceAllocationStatus,
    ResourceAllocationType,
    SubscriptionStatus,
)


class Base(DeclarativeBase):
    pass


def enum_type(
    enum_class: type[PythonEnum],
    name: str,
    *,
    create_constraint: bool = True,
    length: int | None = None,
) -> Enum:
    """Persist StrEnum values as constrained VARCHAR columns."""
    options: dict[str, Any] = {}
    if length is not None:
        options["length"] = length
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=create_constraint,
        validate_strings=True,
        values_callable=lambda members: [str(member.value) for member in members],
        **options,
    )


# =============================================================================
# Business
# =============================================================================


class Business(Base):
    __tablename__ = "businesses"
    __table_args__ = (
        CheckConstraint(
            "location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)",
            name="ck_businesses_lat_range",
        ),
        CheckConstraint(
            "location_lng IS NULL OR (location_lng >= -180 AND location_lng <= 180)",
            name="ck_businesses_lng_range",
        ),
        CheckConstraint(
            "timezone NOT IN ('Factory', 'localtime', 'posixrules') "
            "AND timezone NOT LIKE 'posix/%' AND timezone NOT LIKE 'right/%'",
            name="ck_businesses_timezone_not_special",
        ),
        CheckConstraint(
            "appointment_booking_horizon_days >= 1 AND appointment_booking_horizon_days <= 365",
            name="ck_businesses_appointment_horizon",
        ),
        CheckConstraint(
            "appointment_minimum_notice_minutes >= 0 "
            "AND appointment_minimum_notice_minutes <= 10080",
            name="ck_businesses_appointment_notice",
        ),
        CheckConstraint(
            "appointment_slot_interval_minutes >= 5 AND appointment_slot_interval_minutes <= 120",
            name="ck_businesses_appointment_slot_interval",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    owner_name: Mapped[str | None] = mapped_column(String(200))
    primary_contact_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    location_lat: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    location_lng: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="Asia/Kolkata")
    appointment_booking_horizon_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default="90"
    )
    appointment_minimum_notice_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    appointment_slot_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15, server_default="15"
    )
    phone_number: Mapped[str | None] = mapped_column(String(20))
    subscription: Mapped[str] = mapped_column(
        enum_type(SubscriptionStatus, "subscription_status"),
        nullable=False,
        default=SubscriptionStatus.TRIAL,
    )
    paid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    capabilities: Mapped[list["BusinessCapability"]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    locales: Mapped[list["BusinessLocale"]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    schedules: Mapped[list["OperatingSchedule"]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    schedule_exceptions: Mapped[list["ScheduleException"]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    users: Mapped[list["BusinessUser"]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    products: Mapped[list["Product"]] = relationship(back_populates="business")
    services: Mapped[list["Service"]] = relationship(back_populates="business")
    resources: Mapped[list["Resource"]] = relationship(back_populates="business")
    orders: Mapped[list["Order"]] = relationship(back_populates="business")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="business")
    calls: Mapped[list["Call"]] = relationship(back_populates="business")


class BusinessCapability(Base):
    __tablename__ = "business_capabilities"
    __table_args__ = (UniqueConstraint("business_id", "capability"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    capability: Mapped[str] = mapped_column(
        enum_type(Capability, "capability_type"), nullable=False
    )
    config: Mapped[Any] = mapped_column(JSONB, default=dict)

    business: Mapped["Business"] = relationship(back_populates="capabilities")


class BusinessLocale(Base):
    __tablename__ = "business_locales"
    __table_args__ = (UniqueConstraint("business_id", "locale_code", "role"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    locale_code: Mapped[str] = mapped_column(String(10), nullable=False)
    role: Mapped[str] = mapped_column(enum_type(LocaleRole, "locale_role"), nullable=False)
    voice_speaker: Mapped[str | None] = mapped_column(String(50))
    allow_code_switching: Mapped[bool] = mapped_column(Boolean, default=True)
    validation_status: Mapped[str] = mapped_column(
        enum_type(LanguageStatus, "language_status"),
        nullable=False,
        default=LanguageStatus.EXPERIMENTAL,
    )

    business: Mapped["Business"] = relationship(back_populates="locales")


class BusinessUser(Base):
    """Sole authority for owner/manager authorization."""

    __tablename__ = "business_users"
    __table_args__ = (UniqueConstraint("business_id", "phone"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    role: Mapped[str] = mapped_column(
        enum_type(BusinessUserRole, "business_user_role"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    business: Mapped["Business"] = relationship(back_populates="users")


# =============================================================================
# Schedule
# =============================================================================


class OperatingSchedule(Base):
    __tablename__ = "operating_schedules"
    __table_args__ = (
        CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="ck_schedule_dow"),
        CheckConstraint("close_time > open_time", name="ck_schedule_time_order"),
        Index(
            "uq_schedule_business_scope",
            "business_id",
            "day_of_week",
            "open_time",
            unique=True,
            postgresql_where="resource_id IS NULL",
        ),
        Index(
            "uq_schedule_resource_scope",
            "business_id",
            "resource_id",
            "day_of_week",
            "open_time",
            unique=True,
            postgresql_where="resource_id IS NOT NULL",
        ),
        ForeignKeyConstraint(
            ["business_id", "resource_id"],
            ["resources.business_id", "resources.id"],
            name="fk_operating_schedule_business_resource",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    resource_id: Mapped[int | None] = mapped_column(Integer)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    open_time: Mapped[time] = mapped_column(Time, nullable=False)
    close_time: Mapped[time] = mapped_column(Time, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped["Business"] = relationship(back_populates="schedules")


class ScheduleException(Base):
    __tablename__ = "schedule_exceptions"
    __table_args__ = (
        CheckConstraint(
            "(is_closed AND open_time IS NULL AND close_time IS NULL) OR "
            "(NOT is_closed AND open_time IS NOT NULL AND close_time IS NOT NULL "
            "AND close_time > open_time)",
            name="ck_schedule_exception_consistency",
        ),
        Index(
            "uq_exception_business_scope",
            "business_id",
            "exception_date",
            unique=True,
            postgresql_where="resource_id IS NULL",
        ),
        Index(
            "uq_exception_resource_scope",
            "business_id",
            "resource_id",
            "exception_date",
            unique=True,
            postgresql_where="resource_id IS NOT NULL",
        ),
        ForeignKeyConstraint(
            ["business_id", "resource_id"],
            ["resources.business_id", "resources.id"],
            name="fk_schedule_exception_business_resource",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    resource_id: Mapped[int | None] = mapped_column(Integer)
    exception_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=True)
    open_time: Mapped[time | None] = mapped_column(Time)
    close_time: Mapped[time | None] = mapped_column(Time)
    reason: Mapped[str | None] = mapped_column(String(200))

    business: Mapped["Business"] = relationship(back_populates="schedule_exceptions")


# =============================================================================
# Products, Services, Resources
# =============================================================================


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("business_id", "name"),
        UniqueConstraint("business_id", "id", name="uq_products_business_id_id"),
        CheckConstraint("price_per_unit >= 0", name="ck_product_price"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit: Mapped[str] = mapped_column(
        enum_type(ProductUnit, "product_unit"),
        nullable=False,
        default=ProductUnit.KG,
    )
    price_per_unit: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped["Business"] = relationship(back_populates="products")


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("business_id", "name"),
        UniqueConstraint("business_id", "id", name="uq_services_business_id_id"),
        CheckConstraint(
            "duration_minutes > 0 AND duration_minutes <= 720",
            name="ck_service_duration",
        ),
        CheckConstraint(
            "buffer_before_minutes >= 0 AND buffer_before_minutes <= 240",
            name="ck_service_buffer_before",
        ),
        CheckConstraint(
            "buffer_after_minutes >= 0 AND buffer_after_minutes <= 240",
            name="ck_service_buffer_after",
        ),
        CheckConstraint("price IS NULL OR price >= 0", name="ck_service_price"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    buffer_before_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    buffer_after_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped["Business"] = relationship(back_populates="services")


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("business_id", "name"),
        UniqueConstraint("business_id", "id", name="uq_resources_business_id_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False, default="staff")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped["Business"] = relationship(back_populates="resources")


class ServiceResourceEligibility(Base):
    __tablename__ = "service_resource_eligibility"
    __table_args__ = (
        UniqueConstraint("service_id", "resource_id", name="uq_service_resource_eligibility"),
        ForeignKeyConstraint(
            ["business_id", "service_id"],
            ["services.business_id", "services.id"],
            name="fk_eligibility_business_service",
        ),
        ForeignKeyConstraint(
            ["business_id", "resource_id"],
            ["resources.business_id", "resources.id"],
            name="fk_eligibility_business_resource",
        ),
        Index(
            "ix_eligibility_business_service_active",
            "business_id",
            "service_id",
            "is_active",
        ),
        Index(
            "ix_eligibility_business_resource_active",
            "business_id",
            "resource_id",
            "is_active",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    service_id: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# =============================================================================
# Inventory
# =============================================================================


class InventoryBalance(Base):
    __tablename__ = "inventory_balances"
    __table_args__ = (
        UniqueConstraint("business_id", "product_id", "business_date"),
        CheckConstraint("on_hand_qty >= 0", name="ck_inv_on_hand"),
        CheckConstraint("reserved_qty >= 0", name="ck_inv_reserved"),
        CheckConstraint("reserved_qty <= on_hand_qty", name="ck_inv_reserved_lte_on_hand"),
        CheckConstraint("version > 0", name="ck_inv_balance_version"),
        ForeignKeyConstraint(
            ["business_id", "product_id"],
            ["products.business_id", "products.id"],
            name="fk_inv_balance_business_product",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    on_hand_qty: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    reserved_qty: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    available_tomorrow: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    product: Mapped["Product"] = relationship()

    @property
    def available_qty(self) -> Decimal:
        return self.on_hand_qty - self.reserved_qty


class InventoryReservation(Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "idempotency_key", "product_id", name="uq_inv_res_idempotency"
        ),
        UniqueConstraint("business_id", "id", name="uq_inv_reservations_business_id_id"),
        CheckConstraint("qty > 0", name="ck_inv_res_qty"),
        Index("ix_inv_res_active_expiry", "business_id", "status", "expires_at"),
        ForeignKeyConstraint(
            ["business_id", "product_id"],
            ["products.business_id", "products.id"],
            name="fk_inv_res_business_product",
        ),
        ForeignKeyConstraint(
            ["business_id", "order_id"],
            ["orders.business_id", "orders.id"],
            name="fk_inv_res_business_order",
        ),
        ForeignKeyConstraint(
            ["business_id", "pending_action_id"],
            ["pending_actions.business_id", "pending_actions.id"],
            name="fk_inv_res_business_pending_action",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_action_id: Mapped[int | None] = mapped_column(Integer)
    order_id: Mapped[int | None] = mapped_column(Integer)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        enum_type(InventoryReservationStatus, "reservation_status"),
        nullable=False,
        default=InventoryReservationStatus.ACTIVE,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    product: Mapped["Product"] = relationship()


class InventoryMovement(Base):
    """Append-only ledger. Sign convention:
    - on_hand_delta: positive = stock added, negative = stock removed
    - reserved_delta: positive = stock reserved, negative = reservation released
    """

    __tablename__ = "inventory_movements"
    __table_args__ = (
        UniqueConstraint("business_id", "id", name="uq_inv_movements_business_id_id"),
        CheckConstraint(
            "available_after = on_hand_after - reserved_after",
            name="ck_inv_mov_available_coherence",
        ),
        ForeignKeyConstraint(
            ["business_id", "product_id"],
            ["products.business_id", "products.id"],
            name="fk_inv_mov_business_product",
        ),
        ForeignKeyConstraint(
            ["business_id", "order_id"],
            ["orders.business_id", "orders.id"],
            name="fk_inv_mov_business_order",
        ),
        ForeignKeyConstraint(
            ["business_id", "reservation_id"],
            ["inventory_reservations.business_id", "inventory_reservations.id"],
            name="fk_inv_mov_business_reservation",
        ),
        ForeignKeyConstraint(
            ["business_id", "pending_action_id"],
            ["pending_actions.business_id", "pending_actions.id"],
            name="fk_inv_mov_business_pending_action",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    movement_type: Mapped[str] = mapped_column(
        enum_type(InventoryMovementType, "movement_type"), nullable=False
    )
    on_hand_delta: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    reserved_delta: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    on_hand_after: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    reserved_after: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    available_after: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    order_id: Mapped[int | None] = mapped_column(Integer)
    reservation_id: Mapped[int | None] = mapped_column(Integer)
    pending_action_id: Mapped[int | None] = mapped_column(Integer)
    initiated_by: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product: Mapped["Product"] = relationship()


class InventoryOperation(Base):
    """Durable idempotency record for direct inventory mutations (set/add/walk_in)."""

    __tablename__ = "inventory_operations"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_inv_op_idempotency"),
        CheckConstraint(
            "operation IN ('set', 'add', 'walk_in')",
            name="ck_inv_op_operation",
        ),
        ForeignKeyConstraint(
            ["business_id", "product_id"],
            ["products.business_id", "products.id"],
            name="fk_inv_op_business_product",
        ),
        ForeignKeyConstraint(
            ["business_id", "movement_id"],
            ["inventory_movements.business_id", "inventory_movements.id"],
            name="fk_inv_op_business_movement",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    movement_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# =============================================================================
# Orders
# =============================================================================


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_order_idempotency"),
        UniqueConstraint("business_id", "id", name="uq_orders_business_id_id"),
        UniqueConstraint("pending_action_id", name="uq_order_pending_action"),
        CheckConstraint("total_amount >= 0", name="ck_order_total"),
        Index("ix_orders_business_status", "business_id", "status"),
        ForeignKeyConstraint(
            ["business_id", "pending_action_id"],
            ["pending_actions.business_id", "pending_actions.id"],
            name="fk_order_business_pending_action",
        ),
        ForeignKeyConstraint(
            ["business_id", "call_id"],
            ["calls.business_id", "calls.id"],
            name="fk_order_business_call",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    pickup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        enum_type(OrderStatus, "order_status"),
        nullable=False,
        default=OrderStatus.CONFIRMED,
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    pending_action_id: Mapped[int | None] = mapped_column(Integer)
    call_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    business: Mapped["Business"] = relationship(back_populates="orders")
    line_items: Mapped[list["OrderLineItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderLineItem(Base):
    __tablename__ = "order_line_items"
    __table_args__ = (
        UniqueConstraint("order_id", "product_id", name="uq_line_item_order_product"),
        CheckConstraint("qty > 0", name="ck_line_qty"),
        CheckConstraint("price_per_unit_snapshot >= 0", name="ck_line_price"),
        CheckConstraint("subtotal >= 0", name="ck_line_subtotal"),
        ForeignKeyConstraint(
            ["business_id", "order_id"],
            ["orders.business_id", "orders.id"],
            name="fk_line_item_business_order",
        ),
        ForeignKeyConstraint(
            ["business_id", "product_id"],
            ["products.business_id", "products.id"],
            name="fk_line_item_business_product",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(Integer, nullable=False)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    unit: Mapped[str] = mapped_column(enum_type(ProductUnit, "line_item_unit"), nullable=False)
    price_per_unit_snapshot: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="line_items")
    product: Mapped["Product"] = relationship(overlaps="line_items,order")


# =============================================================================
# Appointments
# =============================================================================


class Appointment(Base):
    """Business appointment facts; capacity is enforced by ResourceAllocation."""

    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_appt_idempotency"),
        UniqueConstraint("business_id", "id", name="uq_appointments_business_id_id"),
        UniqueConstraint("pending_action_id", name="uq_appt_pending_action"),
        UniqueConstraint(
            "business_id",
            "id",
            "pending_action_id",
            name="uq_appt_pending_provenance",
        ),
        ForeignKeyConstraint(
            ["business_id", "service_id"],
            ["services.business_id", "services.id"],
            name="fk_appointment_business_service",
        ),
        ForeignKeyConstraint(
            ["business_id", "resource_id"],
            ["resources.business_id", "resources.id"],
            name="fk_appointment_business_resource",
        ),
        ForeignKeyConstraint(
            ["business_id", "pending_action_id"],
            ["pending_actions.business_id", "pending_actions.id"],
            name="fk_appointment_business_pending_action",
        ),
        ForeignKeyConstraint(
            ["business_id", "call_id"],
            ["calls.business_id", "calls.id"],
            name="fk_appointment_business_call",
        ),
        CheckConstraint("end_at > start_at", name="ck_appt_time_order"),
        CheckConstraint(
            "duration_minutes_snapshot > 0 AND duration_minutes_snapshot <= 720",
            name="ck_appt_duration_snapshot",
        ),
        CheckConstraint(
            "buffer_before_minutes_snapshot >= 0 AND buffer_before_minutes_snapshot <= 240",
            name="ck_appt_buffer_before_snapshot",
        ),
        CheckConstraint(
            "buffer_after_minutes_snapshot >= 0 AND buffer_after_minutes_snapshot <= 240",
            name="ck_appt_buffer_after_snapshot",
        ),
        CheckConstraint(
            "effective_end_at > effective_start_at",
            name="ck_appt_effective_time_order",
        ),
        CheckConstraint(
            "end_at = start_at + make_interval(mins => duration_minutes_snapshot)",
            name="ck_appt_duration_arithmetic",
        ),
        CheckConstraint(
            "effective_start_at = start_at - "
            "make_interval(mins => buffer_before_minutes_snapshot) AND "
            "effective_end_at = end_at + "
            "make_interval(mins => buffer_after_minutes_snapshot)",
            name="ck_appt_effective_arithmetic",
        ),
        CheckConstraint(
            "status IN ('confirmed', 'completed', 'cancelled', 'no_show')",
            name="appointment_status",
        ),
        CheckConstraint(
            "source IN ('customer_conversation', 'owner_manual', 'walk_in')",
            name="ck_appointment_source",
        ),
        CheckConstraint(
            "(source = 'customer_conversation' AND pending_action_id IS NOT NULL) OR "
            "(source IN ('owner_manual', 'walk_in') AND pending_action_id IS NULL)",
            name="ck_appointment_source_provenance",
        ),
        CheckConstraint("version > 0", name="ck_appointment_version"),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL) OR "
            "(status <> 'cancelled' AND cancelled_at IS NULL)",
            name="ck_appointment_status_cancelled_at",
        ),
        Index("ix_appointments_resource_lookup", "resource_id", "start_at", "end_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    service_id: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    duration_minutes_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_before_minutes_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_after_minutes_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    price_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    business_timezone_snapshot: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        enum_type(AppointmentStatus, "appointment_status", create_constraint=False),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        enum_type(
            AppointmentSource,
            "appointment_source",
            create_constraint=False,
            length=21,
        ),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    pending_action_id: Mapped[int | None] = mapped_column(Integer)
    call_id: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rescheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    business: Mapped["Business"] = relationship(back_populates="appointments")


class ResourceAllocation(Base):
    """Single capacity ledger shared by appointments, walk-ins, and owner blocks."""

    __tablename__ = "resource_allocations"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_allocation_idempotency"),
        CheckConstraint(
            "effective_end_at > effective_start_at",
            name="ck_allocation_time_order",
        ),
        CheckConstraint("version > 0", name="ck_allocation_version"),
        CheckConstraint(
            "(allocation_type = 'appointment' AND appointment_id IS NOT NULL "
            "AND pending_action_id IS NOT NULL AND source = 'customer_conversation') OR "
            "(allocation_type = 'manual_appointment' AND appointment_id IS NOT NULL "
            "AND pending_action_id IS NULL AND source = 'owner_manual') OR "
            "(allocation_type = 'walk_in' AND appointment_id IS NOT NULL "
            "AND pending_action_id IS NULL AND source = 'walk_in') OR "
            "(allocation_type = 'owner_block' AND appointment_id IS NULL "
            "AND pending_action_id IS NULL AND source = 'owner_block')",
            name="ck_allocation_type_source_link",
        ),
        ForeignKeyConstraint(
            ["business_id", "resource_id"],
            ["resources.business_id", "resources.id"],
            name="fk_allocation_business_resource",
        ),
        ForeignKeyConstraint(
            ["business_id", "appointment_id"],
            ["appointments.business_id", "appointments.id"],
            name="fk_allocation_business_appointment",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["business_id", "appointment_id", "pending_action_id"],
            [
                "appointments.business_id",
                "appointments.id",
                "appointments.pending_action_id",
            ],
            name="fk_allocation_appointment_pending_provenance",
        ),
        Index(
            "uq_allocation_active_appointment",
            "appointment_id",
            unique=True,
            postgresql_where="appointment_id IS NOT NULL AND status = 'active'",
        ),
        Index(
            "ix_allocation_business_resource_time",
            "business_id",
            "resource_id",
            "effective_start_at",
            "effective_end_at",
        ),
        Index(
            "ix_allocation_business_appointment",
            "business_id",
            "appointment_id",
        ),
        ExcludeConstraint(
            ("business_id", "="),
            ("resource_id", "="),
            (
                func.tstzrange(
                    literal_column("effective_start_at"),
                    literal_column("effective_end_at"),
                    "[)",
                ),
                "&&",
            ),
            where="status = 'active'",
            name="ex_resource_allocations_active_overlap",
            using="gist",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    appointment_id: Mapped[int | None] = mapped_column(Integer)
    pending_action_id: Mapped[int | None] = mapped_column(Integer)
    allocation_type: Mapped[str] = mapped_column(
        enum_type(ResourceAllocationType, "resource_allocation_type"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        enum_type(ResourceAllocationStatus, "resource_allocation_status"),
        nullable=False,
        default=ResourceAllocationStatus.ACTIVE,
        server_default="active",
    )
    source: Mapped[str] = mapped_column(
        enum_type(ResourceAllocationSource, "resource_allocation_source"), nullable=False
    )
    effective_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AppointmentCommit(Base):
    """Immutable committed cancellation or reschedule linked to one PendingAction."""

    __tablename__ = "appointment_commits"
    __table_args__ = (
        UniqueConstraint("pending_action_id", name="uq_appt_commit_pending_action"),
        ForeignKeyConstraint(
            ["business_id", "appointment_id"],
            ["appointments.business_id", "appointments.id"],
            name="fk_appt_commit_business_appointment",
        ),
        ForeignKeyConstraint(
            ["business_id", "pending_action_id"],
            ["pending_actions.business_id", "pending_actions.id"],
            name="fk_appt_commit_business_pending_action",
        ),
        CheckConstraint(
            "jsonb_typeof(before_snapshot) = 'object'",
            name="ck_appt_commit_before_snapshot_object",
        ),
        CheckConstraint(
            "jsonb_typeof(after_snapshot) = 'object'",
            name="ck_appt_commit_after_snapshot_object",
        ),
        Index("ix_appt_commit_business_appointment", "business_id", "appointment_id"),
        Index("ix_appt_commit_business_pending_action", "business_id", "pending_action_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    pending_action_id: Mapped[int] = mapped_column(Integer, nullable=False)
    appointment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(
        enum_type(AppointmentCommitOperation, "appointment_commit_operation"), nullable=False
    )
    before_snapshot: Mapped[Any] = mapped_column(JSONB, nullable=False)
    after_snapshot: Mapped[Any] = mapped_column(JSONB, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# =============================================================================
# Pending Actions
# =============================================================================


class PendingAction(Base):
    """Shared confirmation lifecycle for orders, appointments, owner updates.

    Nothing is committed to inventory/slots until status reaches CONFIRMED
    via deterministic domain code.
    """

    __tablename__ = "pending_actions"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_pending_idempotency"),
        UniqueConstraint("business_id", "id", name="uq_pending_actions_business_id_id"),
        Index("ix_pending_actions_expiry", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(100))
    action_type: Mapped[str] = mapped_column(
        enum_type(PendingActionType, "action_type"), nullable=False
    )
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    proposed_payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_snapshot: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        enum_type(PendingActionStatus, "pending_action_status"),
        nullable=False,
        default=PendingActionStatus.COLLECTING_DETAILS,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    initiated_by: Mapped[str | None] = mapped_column(String(20))
    confirmed_by: Mapped[str | None] = mapped_column(String(20))
    committed_entity_type: Mapped[str | None] = mapped_column(String(50))
    committed_entity_id: Mapped[int | None] = mapped_column(Integer)
    commit_error_code: Mapped[str | None] = mapped_column(String(50))
    commit_error_message: Mapped[str | None] = mapped_column(String(500))
    rejection_reason_code: Mapped[str | None] = mapped_column(String(50))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =============================================================================
# Calls
# =============================================================================


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        UniqueConstraint("business_id", "id", name="uq_calls_business_id_id"),
        CheckConstraint(
            "language_confidence IS NULL OR "
            "(language_confidence >= 0 AND language_confidence <= 1)",
            name="ck_call_lang_confidence",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    caller_phone: Mapped[str | None] = mapped_column(String(20))
    caller_role: Mapped[str | None] = mapped_column(enum_type(CallerRole, "caller_role"))
    detected_language: Mapped[str | None] = mapped_column(String(10))
    language_confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(enum_type(CallOutcome, "call_outcome"))
    transcript: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped["Business"] = relationship(back_populates="calls")


# =============================================================================
# Owner Audit Trail
# =============================================================================


class OwnerAuditLog(Base):
    __tablename__ = "owner_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    initiated_by_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    pending_action_id: Mapped[int | None] = mapped_column(ForeignKey("pending_actions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# =============================================================================
# Onboarding
# =============================================================================


class BusinessOnboardingDraft(Base):
    __tablename__ = "business_onboarding_drafts"
    __table_args__ = (
        UniqueConstraint("business_id", "draft_digest", name="uq_onboarding_draft_digest"),
        CheckConstraint("version > 0", name="ck_onboarding_draft_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(
        enum_type(OnboardingDraftStatus, "onboarding_draft_status"),
        nullable=False,
        default=OnboardingDraftStatus.DRAFT,
    )
    draft_data: Mapped[Any] = mapped_column(JSONB, nullable=False)
    draft_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("business_users.id"))
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("business_users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BusinessConfigurationCommit(Base):
    __tablename__ = "business_configuration_commits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    onboarding_draft_id: Mapped[int] = mapped_column(
        ForeignKey("business_onboarding_drafts.id"), nullable=False
    )
    draft_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    committed_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("business_users.id"), nullable=False
    )
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    commit_evidence: Mapped[Any] = mapped_column(JSONB, nullable=False)
    rollback_evidence: Mapped[Any | None] = mapped_column(JSONB, nullable=True)


# =============================================================================
# Notification Outbox
# =============================================================================


class NotificationOutboxEvent(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_notification_idempotency"),
        Index("ix_notification_outbox_poll", "status", "next_attempt_at"),
        Index(
            "ix_notification_outbox_entity",
            "business_id",
            "entity_type",
            "entity_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(
        enum_type(NotificationEventType, "notification_event_type"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient_type: Mapped[str] = mapped_column(
        enum_type(NotificationRecipientType, "notification_recipient_type"), nullable=False
    )
    recipient_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_name: Mapped[str | None] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(
        enum_type(NotificationChannel, "notification_channel"), nullable=False
    )
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_type(NotificationStatus, "notification_status"),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# =============================================================================
# WhatsApp Durable Inbound Events
# =============================================================================


class WhatsAppDeliveryAttempt(Base):
    __tablename__ = "whatsapp_delivery_attempts"
    __table_args__ = (
        UniqueConstraint(
            "notification_event_id",
            "attempt_number",
            name="uq_whatsapp_delivery_attempt",
        ),
        Index(
            "ix_whatsapp_delivery_attempt_provider_message",
            "provider_message_id",
        ),
        CheckConstraint(
            "status IN ('queued', 'sending', 'accepted', 'delivered', 'failed', 'unknown')",
            name="ck_whatsapp_delivery_attempt_status",
        ),
        CheckConstraint(
            "attempt_number > 0",
            name="ck_whatsapp_delivery_attempt_number",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    notification_event_id: Mapped[int] = mapped_column(
        ForeignKey("notification_outbox.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    error_class: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatsAppInboundEvent(Base):
    __tablename__ = "whatsapp_inbound_events"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_whatsapp_inbound_message_id"),
        Index("ix_whatsapp_inbound_events_poll", "status", "next_attempt_at"),
        CheckConstraint("attempts >= 0", name="ck_whatsapp_inbound_attempts_non_negative"),
        CheckConstraint("max_attempts > 0", name="ck_whatsapp_inbound_max_attempts_positive"),
        CheckConstraint("attempts <= max_attempts", name="ck_whatsapp_inbound_attempts_bounded"),
        CheckConstraint(
            "status IN ('received', 'processing', 'domain_processed', "
            "'completed', 'failed', 'dead_letter', 'response_failed')",
            name="ck_whatsapp_inbound_status_valid",
        ),
        CheckConstraint(
            "(status != 'completed') OR (completed_at IS NOT NULL AND message_body IS NULL)",
            name="ck_whatsapp_inbound_completed_requires_timestamp",
        ),
        CheckConstraint(
            "(status NOT IN ('dead_letter', 'response_failed')) OR (dead_lettered_at IS NOT NULL)",
            name="ck_whatsapp_inbound_dead_letter_requires_timestamp",
        ),
        CheckConstraint(
            "claim_version > 0",
            name="ck_whatsapp_inbound_claim_version_positive",
        ),
        CheckConstraint(
            "length(phone_number_id) > 0",
            name="ck_whatsapp_inbound_phone_number_id_nonempty",
        ),
        CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'processing' AND claim_token IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_whatsapp_inbound_claim_consistency",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(100), nullable=False)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    phone_number_id: Mapped[str] = mapped_column(String(100), nullable=False)
    sender_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    message_type: Mapped[str] = mapped_column(String(20), nullable=False)
    message_body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="received")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    claim_token: Mapped[_uuid.UUID | None] = mapped_column(Uuid)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    provider_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =============================================================================
# Conversations
# =============================================================================


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_phone_lookup", "business_id", "customer_phone", "state"),
        Index("ix_conversations_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    collected_facts: Mapped[Any] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    proposal_id: Mapped[int | None] = mapped_column(Integer)
    proposal_version: Mapped[int | None] = mapped_column(Integer)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DBConversationTurn(Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        Index("ix_conversation_turns_lookup", "conversation_id", "turn_number"),
        UniqueConstraint("conversation_id", "turn_number", name="uq_conversation_turns_conv_turn"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    intent: Mapped[str] = mapped_column(String(30), nullable=False)
    safety_classification: Mapped[str] = mapped_column(String(20), nullable=False)
    user_message_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    assistant_response: Mapped[str] = mapped_column(Text, nullable=False)
    collected_facts_snapshot: Mapped[Any] = mapped_column(JSONB, nullable=False)
    missing_facts: Mapped[Any] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    proposal_id: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# =============================================================================
# Daily Context
# =============================================================================


class BusinessDailyContext(Base):
    __tablename__ = "business_daily_context"
    __table_args__ = (Index("ix_daily_context_lookup", "business_id", "context_date", "active"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    context_date: Mapped[date] = mapped_column(Date, nullable=False)
    context_type: Mapped[str] = mapped_column(
        enum_type(DailyContextType, "daily_context_type"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_by_phone: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
