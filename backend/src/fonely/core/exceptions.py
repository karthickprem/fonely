"""Typed domain exceptions."""


class FonelyError(Exception):
    """Base for all domain errors."""


class NotFoundError(FonelyError):
    """Requested entity does not exist."""


class UnauthorizedError(FonelyError):
    """Caller is not authorized for this operation."""


class ValidationError(FonelyError):
    """Input failed domain validation."""


class InvalidStateTransitionError(FonelyError):
    """Attempted an illegal state transition."""


class InsufficientStockError(FonelyError):
    """Not enough inventory to fulfill the request."""

    def __init__(self, product_name: str, requested: str, available: str) -> None:
        self.product_name = product_name
        self.requested = requested
        self.available = available
        super().__init__(
            f"Insufficient stock for {product_name}: requested {requested}, available {available}"
        )


class IdempotencyConflictError(FonelyError):
    """An action with this idempotency key already exists."""

    def __init__(self, existing_id: int) -> None:
        self.existing_id = existing_id
        super().__init__(f"Action already exists with id={existing_id}")


class ReservationExpiredError(FonelyError):
    """The reservation or pending action has expired."""


class ResourceConflictError(FonelyError):
    """The requested resource/time is no longer available."""
