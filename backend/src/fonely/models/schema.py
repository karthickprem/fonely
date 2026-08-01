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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from fonely.models.enums import (
    AppointmentStatus,
    BusinessUserRole,
    CallerRole,
    CallOutcome,
    Capability,
    InventoryMovementType,
    InventoryReservationStatus,
    LanguageStatus,
    LocaleRole,
    OrderStatus,
    PendingActionStatus,
    PendingActionType,
    ProductUnit,
    SubscriptionStatus,
)


class Base(DeclarativeBase):
    pass


def enum_type(enum_class: type[PythonEnum], name: str) -> Enum:
    """Persist StrEnum values as constrained VARCHAR columns."""
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [str(member.value) for member in members],
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
        UniqueConstraint("business_id", "day_of_week", "open_time"),
        CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="ck_schedule_dow"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    open_time: Mapped[time] = mapped_column(Time, nullable=False)
    close_time: Mapped[time] = mapped_column(Time, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped["Business"] = relationship(back_populates="schedules")


class ScheduleException(Base):
    __tablename__ = "schedule_exceptions"
    __table_args__ = (UniqueConstraint("business_id", "exception_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
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
        CheckConstraint("duration_minutes > 0", name="ck_service_duration"),
        CheckConstraint("price IS NULL OR price >= 0", name="ck_service_price"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped["Business"] = relationship(back_populates="services")


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (UniqueConstraint("business_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False, default="staff")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped["Business"] = relationship(back_populates="resources")


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
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
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
        CheckConstraint("qty > 0", name="ck_inv_res_qty"),
        Index("ix_inv_res_active_expiry", "business_id", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    pending_action_id: Mapped[int | None] = mapped_column(ForeignKey("pending_actions.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
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

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    movement_type: Mapped[str] = mapped_column(
        enum_type(InventoryMovementType, "movement_type"), nullable=False
    )
    on_hand_delta: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    reserved_delta: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    on_hand_after: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    reserved_after: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    available_after: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    reservation_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_reservations.id"))
    pending_action_id: Mapped[int | None] = mapped_column(ForeignKey("pending_actions.id"))
    initiated_by: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product: Mapped["Product"] = relationship()


# =============================================================================
# Orders
# =============================================================================


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_order_idempotency"),
        UniqueConstraint("pending_action_id", name="uq_order_pending_action"),
        CheckConstraint("total_amount >= 0", name="ck_order_total"),
        Index("ix_orders_business_status", "business_id", "status"),
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
    pending_action_id: Mapped[int | None] = mapped_column(ForeignKey("pending_actions.id"))
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    business: Mapped["Business"] = relationship(back_populates="orders")
    line_items: Mapped[list["OrderLineItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderLineItem(Base):
    __tablename__ = "order_line_items"
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_line_qty"),
        CheckConstraint("price_per_unit_snapshot >= 0", name="ck_line_price"),
        CheckConstraint("subtotal >= 0", name="ck_line_subtotal"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    product_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    unit: Mapped[str] = mapped_column(enum_type(ProductUnit, "line_item_unit"), nullable=False)
    price_per_unit_snapshot: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="line_items")
    product: Mapped["Product"] = relationship()


# =============================================================================
# Appointments
# =============================================================================


class Appointment(Base):
    """Appointment against a specific resource.

    ix_appointments_resource_lookup is a query acceleration index only.
    It does NOT prevent time overlap. PostgreSQL exclusion constraint:
      EXCLUDE USING gist (resource_id WITH =, tstzrange(start_at, end_at) WITH &&)
      WHERE (status IN ('held', 'confirmed'))
    will be added in the appointment domain phase.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_appt_idempotency"),
        UniqueConstraint("pending_action_id", name="uq_appt_pending_action"),
        CheckConstraint("end_at > start_at", name="ck_appt_time_order"),
        Index("ix_appointments_resource_lookup", "resource_id", "start_at", "end_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id"), nullable=False)
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"))
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        enum_type(AppointmentStatus, "appointment_status"),
        nullable=False,
        default=AppointmentStatus.HELD,
    )
    held_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    pending_action_id: Mapped[int | None] = mapped_column(ForeignKey("pending_actions.id"))
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    business: Mapped["Business"] = relationship(back_populates="appointments")
    resource: Mapped["Resource"] = relationship()
    service: Mapped["Service | None"] = relationship()


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
