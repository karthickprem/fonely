"""Domain enums — used for database constraints and Python-level validation."""

import enum


class Capability(enum.StrEnum):
    INVENTORY = "inventory"
    APPOINTMENTS = "appointments"


class LocaleRole(enum.StrEnum):
    OWNER = "owner"
    DEFAULT = "default"
    SUPPORTED = "supported"


class LanguageStatus(enum.StrEnum):
    EXPERIMENTAL = "experimental"
    BETA = "beta"
    VERIFIED = "verified"


class SubscriptionStatus(enum.StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class PendingActionType(enum.StrEnum):
    ORDER = "order"
    APPOINTMENT = "appointment"
    OWNER_STOCK_UPDATE = "owner_stock_update"
    OWNER_PRICE_UPDATE = "owner_price_update"
    OWNER_SCHEDULE_UPDATE = "owner_schedule_update"


class PendingActionStatus(enum.StrEnum):
    COLLECTING_DETAILS = "collecting_details"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMMITTING = "committing"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class OrderStatus(enum.StrEnum):
    CONFIRMED = "confirmed"
    PICKED_UP = "picked_up"
    CANCELLED = "cancelled"


class AppointmentStatus(enum.StrEnum):
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class AppointmentSource(enum.StrEnum):
    CUSTOMER_CONVERSATION = "customer_conversation"
    OWNER_MANUAL = "owner_manual"
    WALK_IN = "walk_in"


class AppointmentCommitOperation(enum.StrEnum):
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"


class ResourceAllocationType(enum.StrEnum):
    APPOINTMENT = "appointment"
    MANUAL_APPOINTMENT = "manual_appointment"
    WALK_IN = "walk_in"
    OWNER_BLOCK = "owner_block"


class ResourceAllocationStatus(enum.StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    CANCELLED = "cancelled"


class ResourceAllocationSource(enum.StrEnum):
    CUSTOMER_CONVERSATION = "customer_conversation"
    OWNER_MANUAL = "owner_manual"
    WALK_IN = "walk_in"
    OWNER_BLOCK = "owner_block"


class InventoryReservationStatus(enum.StrEnum):
    ACTIVE = "active"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"


class InventoryMovementType(enum.StrEnum):
    STOCK_ADDED = "stock_added"
    WALK_IN_SALE = "walk_in_sale"
    PHONE_ORDER_RESERVED = "phone_order_reserved"
    RESERVATION_RELEASED = "reservation_released"
    ORDER_COMPLETED = "order_completed"
    ORDER_CANCELLED = "order_cancelled"
    MANUAL_ADJUSTMENT = "manual_adjustment"


class ProductUnit(enum.StrEnum):
    KG = "kg"
    GRAM = "gram"
    PIECE = "piece"
    DOZEN = "dozen"
    LITRE = "litre"
    ML = "ml"
    PACKET = "packet"


class CallOutcome(enum.StrEnum):
    ORDERED = "ordered"
    BOOKED = "booked"
    ENQUIRY = "enquiry"
    OUT_OF_STOCK = "out_of_stock"
    ESCALATED = "escalated"
    NO_ACTION = "no_action"
    DROPPED = "dropped"


class CallerRole(enum.StrEnum):
    """Call-context classification — who is on this call."""

    OWNER = "owner"
    MANAGER = "manager"
    CUSTOMER = "customer"


class BusinessUserRole(enum.StrEnum):
    """Authorization membership — who can mutate business state."""

    OWNER = "owner"
    MANAGER = "manager"


class OnboardingDraftStatus(enum.StrEnum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    ACTIVATED = "activated"
    REJECTED = "rejected"
