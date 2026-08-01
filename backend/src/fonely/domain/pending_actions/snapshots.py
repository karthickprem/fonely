"""Canonical serialization, stable digest, and confirmation snapshots."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from fonely.domain.pending_actions.payloads import PayloadEnvelope


class ConfirmationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    action_type: str
    facts: dict[str, Any]


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return format(normalized, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Cannot canonicalize naive datetime")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value"):
        return _canonical_value(value.value)
    return value


def canonical_payload_dict(payload: PayloadEnvelope) -> dict[str, Any]:
    result = _canonical_value(payload)
    assert isinstance(result, dict)
    return result


def canonical_json(payload: PayloadEnvelope) -> str:
    return json.dumps(
        canonical_payload_dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_digest(payload: PayloadEnvelope) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def idempotency_matches(
    *,
    existing_action_type: str,
    existing_digest: str,
    proposed_action_type: str,
    proposed_digest: str,
) -> bool:
    return existing_action_type == proposed_action_type and existing_digest == proposed_digest


def confirmation_snapshot(payload: PayloadEnvelope) -> str:
    canonical = canonical_payload_dict(payload)
    snapshot = ConfirmationSnapshot(
        schema_version=payload.schema_version,
        action_type=str(payload.action_type),
        facts=canonical["data"],
    )
    return json.dumps(
        _canonical_value(snapshot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
