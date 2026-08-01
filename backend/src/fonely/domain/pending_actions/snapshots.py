"""Canonical serialization, stable digest, and confirmation snapshots."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from fonely.domain.appointments.datetimes import require_aware
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
        require_aware(value, label="Canonical datetime")
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
    facts = canonical["data"]
    if str(payload.action_type) == "appointment":
        operation = facts["operation"]
        if operation == "create":
            appointment_facts = facts["facts"]
        elif operation == "cancel":
            appointment_facts = facts["current_facts"]
        else:
            appointment_facts = facts["new_facts"]
        facts = {
            "operation": operation,
            "service_id": appointment_facts["service_id"],
            "service_name": appointment_facts["service_name"],
            "resource_id": appointment_facts["resource_id"],
            "resource_name": appointment_facts["resource_name"],
            "start_at": appointment_facts["start_at"],
            "end_at": appointment_facts["end_at"],
            "duration_minutes": appointment_facts["duration_minutes"],
            "price": appointment_facts["price"],
            "business_timezone": appointment_facts["business_timezone"],
        }
        if operation in {"cancel", "reschedule"}:
            facts["target_appointment_id"] = canonical["data"]["target_appointment_id"]
        if operation == "cancel":
            facts["reason_code"] = canonical["data"]["reason_code"]
        elif operation == "reschedule":
            old_facts = canonical["data"]["old_facts"]
            facts["old_facts"] = {
                "service_id": old_facts["service_id"],
                "service_name": old_facts["service_name"],
                "resource_id": old_facts["resource_id"],
                "resource_name": old_facts["resource_name"],
                "start_at": old_facts["start_at"],
                "end_at": old_facts["end_at"],
                "duration_minutes": old_facts["duration_minutes"],
                "price": old_facts["price"],
                "business_timezone": old_facts["business_timezone"],
            }
    snapshot = ConfirmationSnapshot(
        schema_version=payload.schema_version,
        action_type=str(payload.action_type),
        facts=facts,
    )
    return json.dumps(
        _canonical_value(snapshot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
