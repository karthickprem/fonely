"""Typed order-domain errors with stable mapping codes."""

from fonely.core.exceptions import FonelyError


class OrderError(FonelyError):
    code = "order_error"


class OrderNotFoundError(OrderError):
    code = "order_not_found"


class OrderUnauthorizedError(OrderError):
    code = "order_unauthorized"


class OrderValidationError(OrderError):
    code = "order_validation_failed"


class OrderStateTransitionError(OrderError):
    code = "order_state_transition_invalid"


class OrderIdempotencyConflictError(OrderError):
    code = "order_idempotency_conflict"


class OrderTenantMismatchError(OrderError):
    code = "order_tenant_mismatch"


class OrderStaleVersionError(OrderError):
    code = "order_stale_version"


class OrderReservationExpiredError(OrderError):
    code = "order_reservation_expired"


class OrderTotalOverflowError(OrderError):
    code = "order_total_overflow"
