"""Typed safe results returned by PendingAction services."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from fonely.models.enums import PendingActionStatus, PendingActionType


class PendingActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    business_id: int
    action_type: PendingActionType
    status: PendingActionStatus
    payload_schema_version: int
    payload: dict[str, Any]
    payload_digest: str
    confirmation_snapshot: str | None
    expires_at: datetime
    version: int
    committed_entity_type: str | None
    committed_entity_id: int | None
    error_code: str | None
    rejection_reason_code: str | None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None


class BulkExpiryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expired_ids: tuple[int, ...]
    count: int
