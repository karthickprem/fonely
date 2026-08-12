"""Strict commands and contexts for PendingAction operations."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from fonely.core.validators import AwareDatetime, E164PhoneNumber
from fonely.models.enums import CallerRole, Channel, PendingActionType


class StrictCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ActorContext(StrictCommand):
    """Verified caller context constructed by the application, never an LLM."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    business_id: Annotated[int, Field(gt=0)]
    normalized_phone: E164PhoneNumber
    verified_role: CallerRole
    # Transport the caller reached us on. REQUIRED, no default: a defaulted
    # trust-boundary field would make "the voice path forgot to set it"
    # indistinguishable from "this is genuinely text", and the silent failure
    # lands on a patient (a connected voice caller told to call the clinic).
    # Every construction site must state it explicitly (CEO #33 amendment 1).
    channel: Channel
    session_id: Annotated[str | None, Field(default=None, max_length=100)]


class CommitResultContext(StrictCommand):
    """Trusted internal engine identity for commit lifecycle operations."""

    business_id: Annotated[int, Field(gt=0)]
    pending_action_id: Annotated[int, Field(gt=0)]
    expected_version: Annotated[int, Field(gt=0)]
    engine: Literal["order_engine", "appointment_engine", "inventory_engine"]


class CreatePendingActionCommand(StrictCommand):
    actor: ActorContext
    action_type: PendingActionType
    payload_schema_version: Annotated[int, Field(gt=0)] = 1
    payload: dict[str, object]
    expires_at: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]


class RevisePendingActionCommand(StrictCommand):
    actor: ActorContext
    action_id: Annotated[int, Field(gt=0)]
    expected_version: Annotated[int, Field(gt=0)]
    payload_schema_version: Annotated[int, Field(gt=0)] = 1
    payload: dict[str, object]


class MarkAwaitingConfirmationCommand(StrictCommand):
    actor: ActorContext
    action_id: Annotated[int, Field(gt=0)]
    expected_version: Annotated[int, Field(gt=0)]


class BeginCommitCommand(StrictCommand):
    context: CommitResultContext


class CompleteCommitCommand(StrictCommand):
    context: CommitResultContext
    committed_entity_type: Literal["order", "appointment", "appointment_commit", "inventory_update"]
    committed_entity_id: Annotated[int, Field(gt=0)]


class FailCommitCommand(StrictCommand):
    context: CommitResultContext
    error_code: Literal[
        "temporary_conflict",
        "insufficient_stock",
        "invalid_product",
        "resource_unavailable",
        "transaction_failed",
    ]
    retryable: bool


class RejectPendingActionCommand(StrictCommand):
    actor: ActorContext
    action_id: Annotated[int, Field(gt=0)]
    expected_version: Annotated[int, Field(gt=0)]
    reason_code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,49}$")]


class CancelPendingActionCommand(StrictCommand):
    actor: ActorContext
    action_id: Annotated[int, Field(gt=0)]
    expected_version: Annotated[int, Field(gt=0)]


class ExpirePendingActionCommand(StrictCommand):
    business_id: Annotated[int, Field(gt=0)]
    action_id: Annotated[int, Field(gt=0)]
    expected_version: Annotated[int, Field(gt=0)]
    now: AwareDatetime


class BulkExpirePendingActionsCommand(StrictCommand):
    now: AwareDatetime
    batch_size: Annotated[int, Field(gt=0, le=1000)] = 100


class GetPendingActionQuery(StrictCommand):
    actor: ActorContext
    action_id: Annotated[int, Field(gt=0)]


class GetActivePendingActionQuery(StrictCommand):
    actor: ActorContext
    session_id: Annotated[str, Field(min_length=1, max_length=100)]
    action_type: PendingActionType | None = None


class InternalGetPendingActionQuery(StrictCommand):
    business_id: Annotated[int, Field(gt=0)]
    action_id: Annotated[int, Field(gt=0)]


class InternalGetActivePendingActionQuery(StrictCommand):
    business_id: Annotated[int, Field(gt=0)]
    session_id: Annotated[str, Field(min_length=1, max_length=100)]
    action_type: PendingActionType | None = None
    now: AwareDatetime
