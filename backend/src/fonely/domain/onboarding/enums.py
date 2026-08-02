"""Onboarding-local enums for Stage A canonical draft."""

from enum import IntEnum, StrEnum


class DraftStatus(StrEnum):
    INTAKE = "intake"
    VALIDATING = "validating"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    ACTIVATION_READY = "activation_ready"


class ReviewStatus(StrEnum):
    CLEAR = "clear"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    UNREADABLE = "unreadable"
    UNSUPPORTED = "unsupported"
    OWNER_CONFIRMED = "owner_confirmed"
    OWNER_CORRECTED = "owner_corrected"


class PriceKind(StrEnum):
    FIXED = "fixed"
    STARTING_FROM = "starting_from"
    RANGE = "range"
    VARIABLE = "variable"
    CONSULTATION_REQUIRED = "consultation_required"
    NOT_PROVIDED = "not_provided"


class SourceType(StrEnum):
    OPERATOR_ENTRY = "operator_entry"
    OWNER_FORM = "owner_form"
    WHATSAPP_TEXT = "whatsapp_text"
    VOICE_NOTE = "voice_note"
    IMAGE = "image"
    PDF = "pdf"
    CSV = "csv"
    SPREADSHEET = "spreadsheet"
    WEBSITE = "website"
    EXTERNAL_SYSTEM = "external_system"


class BusinessCategory(StrEnum):
    SALON = "salon"
    CLINIC = "clinic"
    TUTORING = "tutoring"
    SHOP = "shop"
    BAKERY = "bakery"
    FLORIST = "florist"
    GENERAL_SERVICE = "general_service"
    GENERAL_RETAIL = "general_retail"


class ResourceType(StrEnum):
    STAFF = "staff"
    ROOM = "room"
    CHAIR = "chair"
    EQUIPMENT = "equipment"
    OTHER = "other"


class IssueSeverity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"


class IssueCategory(StrEnum):
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    UNREADABLE = "unreadable"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"
    DUPLICATE = "duplicate"
    CROSS_REFERENCE = "cross_reference"


class ActivationDecision(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"
    BLOCKED_UNSUPPORTED = "blocked_unsupported"
    REQUIRES_TEST_MODE = "requires_test_mode_validation"


class QuestionAudience(StrEnum):
    OWNER = "owner"
    OPERATOR = "operator"


class Weekday(IntEnum):
    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6
