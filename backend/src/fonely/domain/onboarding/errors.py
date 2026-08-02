"""Onboarding domain errors."""

from fonely.core.exceptions import FonelyError


class OnboardingError(FonelyError):
    pass


class DraftConstructionError(OnboardingError):
    pass


class StaleApprovalError(OnboardingError):
    def __init__(self, approved_digest: str, current_digest: str) -> None:
        self.approved_digest = approved_digest
        self.current_digest = current_digest
        super().__init__(
            f"Approval digest {approved_digest} does not match current {current_digest}"
        )


class UnresolvedBlockersError(OnboardingError):
    def __init__(self, blocker_count: int) -> None:
        self.blocker_count = blocker_count
        super().__init__(f"{blocker_count} unresolved blocker(s) prevent approval")


class DraftLimitExceededError(OnboardingError):
    def __init__(self, field: str, limit: int, actual: int) -> None:
        self.field = field
        self.limit = limit
        self.actual = actual
        super().__init__(f"{field}: limit {limit}, got {actual}")


class InvalidReviewerError(OnboardingError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid reviewer reference: {reason}")
