"""Safe appointment-domain errors and public error codes."""

import enum


class AppointmentErrorCode(enum.StrEnum):
    NOT_FOUND = "not_found"
    AUTHORIZATION_DENIED = "authorization_denied"
    SERVICE_INACTIVE = "service_inactive"
    RESOURCE_INACTIVE = "resource_inactive"
    RESOURCE_INELIGIBLE = "resource_ineligible"
    OUTSIDE_WORKING_HOURS = "outside_working_hours"
    SLOT_CONFLICT = "slot_conflict"
    EXPIRED = "expired"
    STALE_VERSION = "stale_version"
    INVALID_STATE = "invalid_state"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CONFIGURATION_ERROR = "configuration_error"
    TRANSACTION_FAILED = "transaction_failed"


_INTERNAL_TO_PUBLIC = {"resource_unavailable": AppointmentErrorCode.SLOT_CONFLICT}


def public_error_code(internal_code: str) -> AppointmentErrorCode:
    """Map an internal engine code to a caller-safe stable code."""
    mapped = _INTERNAL_TO_PUBLIC.get(internal_code)
    if mapped is not None:
        return mapped
    try:
        return AppointmentErrorCode(internal_code)
    except ValueError:
        return AppointmentErrorCode.TRANSACTION_FAILED


class AppointmentDomainError(Exception):
    def __init__(self, code: AppointmentErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)
