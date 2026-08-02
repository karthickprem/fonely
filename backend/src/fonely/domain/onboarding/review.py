"""Owner review contract and activation readiness."""

from __future__ import annotations

from fonely.domain.onboarding.enums import ActivationDecision, IssueSeverity
from fonely.domain.onboarding.errors import (
    InvalidReviewerError,
    StaleApprovalError,
    UnresolvedBlockersError,
)
from fonely.domain.onboarding.limits import SCHEMA_VERSION
from fonely.domain.onboarding.models import BusinessOnboardingDraft, validate_reviewer_ref
from fonely.domain.onboarding.results import (
    ActivationReadinessResult,
    ApprovalResult,
    ReviewProposal,
)
from fonely.domain.onboarding.validation import validate_draft


def create_review_proposal(draft: BusinessOnboardingDraft) -> ReviewProposal:
    result = validate_draft(draft)
    return ReviewProposal(
        draft_digest=result.draft_digest,
        blocker_count=result.blocker_count,
        warning_count=result.warning_count,
        can_approve=result.blocker_count == 0,
        issues=result.issues,
    )


def approve_draft(
    draft: BusinessOnboardingDraft,
    *,
    reviewer_ref: str,
    expected_digest: str,
) -> ApprovalResult:
    try:
        validated_ref = validate_reviewer_ref(reviewer_ref)
    except ValueError as exc:
        raise InvalidReviewerError(str(exc)) from exc

    current_digest = draft.canonical_digest()
    if expected_digest != current_digest:
        raise StaleApprovalError(expected_digest, current_digest)

    result = validate_draft(draft)
    if result.blocker_count > 0:
        raise UnresolvedBlockersError(result.blocker_count)

    return ApprovalResult(
        approved=True,
        draft_digest=current_digest,
        reviewer_ref=validated_ref,
        blocker_count=0,
    )


def check_activation_readiness(
    draft: BusinessOnboardingDraft,
    *,
    approved_digest: str | None,
    reviewer_ref: str | None,
) -> ActivationReadinessResult:
    current_digest = draft.canonical_digest()

    if draft.schema_version != SCHEMA_VERSION:
        return ActivationReadinessResult(
            decision=ActivationDecision.BLOCKED_UNSUPPORTED,
            draft_digest=current_digest,
            approved_digest=approved_digest,
            blocker_count=1,
            reasons=("Unsupported schema version",),
        )

    result = validate_draft(draft)
    actual_blockers = result.blocker_count

    if reviewer_ref is not None:
        try:
            validate_reviewer_ref(reviewer_ref)
        except ValueError:
            actual_blockers += 1

    reasons: list[str] = []
    if approved_digest is None or reviewer_ref is None:
        actual_blockers += 1
        reasons.append("Draft has not been approved by an owner")
    elif approved_digest != current_digest:
        actual_blockers += 1
        reasons.append("Draft changed since approval; re-approval required")

    if result.blocker_count > 0:
        reasons.append(f"{result.blocker_count} validation blocker(s) remain")

    has_unsupported = any(
        i.category.value == "unsupported" and i.severity is IssueSeverity.BLOCKER
        for i in result.issues
    )
    if has_unsupported:
        return ActivationReadinessResult(
            decision=ActivationDecision.BLOCKED_UNSUPPORTED,
            draft_digest=current_digest,
            approved_digest=approved_digest,
            blocker_count=actual_blockers,
            reasons=tuple(reasons),
        )

    if actual_blockers > 0:
        return ActivationReadinessResult(
            decision=ActivationDecision.NOT_READY,
            draft_digest=current_digest,
            approved_digest=approved_digest,
            blocker_count=actual_blockers,
            reasons=tuple(reasons),
        )

    return ActivationReadinessResult(
        decision=ActivationDecision.REQUIRES_TEST_MODE,
        draft_digest=current_digest,
        approved_digest=approved_digest,
        blocker_count=0,
        reasons=("Requires test-mode validation before production activation",),
    )
