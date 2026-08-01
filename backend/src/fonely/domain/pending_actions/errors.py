"""Typed errors for the PendingAction lifecycle."""

from fonely.core.exceptions import FonelyError
from fonely.models.enums import PendingActionStatus


class PendingActionError(FonelyError):
    code = "pending_action_error"


class PendingActionNotFoundError(PendingActionError):
    code = "pending_action_not_found"


class PendingActionUnauthorizedError(PendingActionError):
    code = "pending_action_unauthorized"


class PendingActionValidationError(PendingActionError):
    code = "pending_action_validation_failed"


class PendingActionConcurrencyError(PendingActionError):
    code = "pending_action_stale_version"


class PendingActionExpiredError(PendingActionError):
    code = "pending_action_expired"


class PendingActionIdempotencyConflictError(PendingActionError):
    code = "pending_action_idempotency_conflict"


class UnsupportedPayloadSchemaError(PendingActionError):
    code = "unsupported_payload_schema"


class InvalidStateTransitionError(PendingActionError):
    code = "invalid_state_transition"

    def __init__(
        self,
        current: PendingActionStatus,
        requested: PendingActionStatus,
    ) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"Transition from {current.value} to {requested.value} is not permitted")


class CommitEntityConflictError(PendingActionError):
    code = "committed_entity_conflict"


class TrustedCommitContextError(PendingActionError):
    code = "trusted_commit_context_invalid"
