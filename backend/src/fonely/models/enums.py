"""Domain enums — used for database constraints and Python-level validation."""

import enum


class Channel(enum.StrEnum):
    """The transport a caller reached us on — an authoritative fact set by the
    admission/transport layer, never inferred from model output or caller text.

    It selects channel-specific terminal wording (e.g. a give-up message): on
    TEXT the patient can be told to call the clinic; on VOICE they are already
    connected, so that instruction would be incoherent (CEO #33).
    """

    TEXT = "text"
    VOICE = "voice"


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
    # A durable follow-up record left when the agent gives up mid-booking on a
    # voice call (e.g. doctor/slot disambiguation exhausted) so the caller can be
    # called back to complete the booking. Carries the partial booking facts, not
    # the raw dialogue. Never commits an entity of its own.
    CALLBACK = "callback"


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


class NotificationEventType(enum.StrEnum):
    APPOINTMENT_CONFIRMED = "appointment_confirmed"
    APPOINTMENT_CANCELLED = "appointment_cancelled"
    APPOINTMENT_RESCHEDULED = "appointment_rescheduled"
    APPOINTMENT_REMINDER = "appointment_reminder"
    WHATSAPP_INBOUND_RESPONSE = "whatsapp_inbound_response"
    # Owner-facing push when a voice caller couldn't finish booking and a callback
    # was persisted (#36/#41). Notifies the OWNER only (a callback is a follow-up
    # the clinic owes the caller, not something the caller is told). Unlike
    # appointment_* events this carries NO appointment manifest — see
    # NotificationService.create_callback_notification for why (nudge-grade
    # durability, no appointment_id, deliberately not manifest-wrapped).
    CALLBACK_REQUESTED = "callback_requested"


class NotificationRecipientType(enum.StrEnum):
    PATIENT = "patient"
    OWNER = "owner"
    STAFF = "staff"


class NotificationChannel(enum.StrEnum):
    WHATSAPP = "whatsapp"
    SMS = "sms"
    INTERNAL = "internal"


class NotificationStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    UNKNOWN = "unknown"


class InboundEventStatus(enum.StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    DOMAIN_PROCESSED = "domain_processed"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    RESPONSE_FAILED = "response_failed"


class DailyContextType(enum.StrEnum):
    OFFER = "offer"
    NOTE = "note"
    ANNOUNCEMENT = "announcement"
