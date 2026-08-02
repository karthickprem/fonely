"""Immutable results for onboarding persistence operations."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class OnboardingDraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    business_id: int
    status: str
    draft_digest: str
    version: int
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    activated_at: datetime | None = None
    idempotent_replay: bool = False


class ActivationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    services_count: int
    resources_count: int
    eligibilities_count: int
    schedules_count: int
    exceptions_count: int


class ActivationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_id: int
    business_id: int
    success: bool
    commit_id: int | None = None
    evidence: ActivationEvidence | None = None
    error: str | None = None
