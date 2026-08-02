"""Strict commands for onboarding persistence operations."""

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class OnboardingCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SubmitDraftCommand(OnboardingCommand):
    business_id: Annotated[int, Field(gt=0)]
    actor_user_id: Annotated[int | None, Field(default=None, gt=0)]
    draft_data: dict[str, Any]


class DraftTransitionCommand(OnboardingCommand):
    business_id: Annotated[int, Field(gt=0)]
    draft_id: Annotated[int, Field(gt=0)]
    actor_user_id: Annotated[int, Field(gt=0)]
    expected_version: Annotated[int, Field(gt=0)]


class GetDraftQuery(OnboardingCommand):
    business_id: Annotated[int, Field(gt=0)]
    draft_id: Annotated[int, Field(gt=0)]
