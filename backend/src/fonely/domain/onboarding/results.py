"""Typed result models for onboarding validation, review, and activation."""

from pydantic import BaseModel, ConfigDict

from fonely.domain.onboarding.enums import (
    ActivationDecision,
    IssueCategory,
    IssueSeverity,
    QuestionAudience,
)


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    code: str
    severity: IssueSeverity
    category: IssueCategory
    message: str


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issues: tuple[ValidationIssue, ...]
    blockers: tuple[ValidationIssue, ...]
    warnings: tuple[ValidationIssue, ...]
    blocker_count: int
    warning_count: int
    draft_digest: str


class QuestionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str
    path: str
    code: str
    audience: QuestionAudience
    semantic_intent: str
    related_issue_codes: tuple[str, ...]
    priority: int


class QuestionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    questions: tuple[QuestionIntent, ...]
    blocker_question_count: int
    warning_question_count: int
    draft_digest: str
    omitted_count: int = 0


class ReviewProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_digest: str
    blocker_count: int
    warning_count: int
    can_approve: bool
    issues: tuple[ValidationIssue, ...]


class ApprovalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: bool
    draft_digest: str
    reviewer_ref: str
    blocker_count: int


class ActivationReadinessResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ActivationDecision
    draft_digest: str
    approved_digest: str | None
    blocker_count: int
    reasons: tuple[str, ...]
