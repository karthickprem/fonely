"""Unit contracts for typed immutable owner-command terminal replay evidence."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from fonely.services.owner_commands import (
    OwnerCommandOutcomeEvidence,
    OwnerCommandService,
)


def _evidence() -> OwnerCommandOutcomeEvidence:
    return OwnerCommandOutcomeEvidence(
        schema_version=1,
        operation="doctor_leave",
        proposal_id="proposal-1",
        proposal_version=3,
        business_id=1,
        payload_digest="a" * 64,
        resolved_date="2026-08-10",
        clinic_timezone="Asia/Kolkata",
        appointment_ids=[10, 11],
        affected_appointments=2,
        affected_patients=2,
        schedule_exception={
            "id": 5,
            "resource_id": 1,
            "exception_date": "2026-08-10",
            "is_closed": True,
            "open_time": None,
            "close_time": None,
            "reason": "Leave",
        },
        queued_outbox_ids=[100, 101, 102, 103],
        queued_outbox_count=4,
        audit_id=7,
        completed_at=datetime(2026, 8, 9, 10, tzinfo=UTC).isoformat(),
        presentation_text="Done. 2 appointment(s) cancelled on 2026-08-10. Notifications queued.",
    )


def _proposal(evidence: object) -> MagicMock:
    return MagicMock(
        id="proposal-1",
        business_id=1,
        command_type="doctor_leave",
        payload_digest="a" * 64,
        result_evidence=evidence,
    )


def test_completed_owner_evidence_replays_exact_structured_result() -> None:
    result = OwnerCommandService._result_from_completed_proposal(
        _proposal(_evidence().model_dump(mode="json"))
    )

    assert result.success is True
    assert result.command_type == "doctor_leave"
    assert result.proposal_id == "proposal-1"
    assert result.affected_appointments == 2
    assert result.affected_patients == 2
    assert "Notifications queued" in result.response_text


@pytest.mark.parametrize(
    "mutator",
    [
        lambda data: data.pop("schema_version"),
        lambda data: data.update({"schema_version": 99}),
        lambda data: data.update({"proposal_id": "other"}),
        lambda data: data.update({"business_id": 2}),
        lambda data: data.update({"payload_digest": "b" * 64}),
        lambda data: data.update({"status": "failed"}),
        lambda data: data.update({"unknown": "field"}),
    ],
)
def test_completed_owner_evidence_corruption_fails_closed(mutator: object) -> None:
    data = _evidence().model_dump(mode="json")
    mutator(data)  # type: ignore[operator]

    with pytest.raises(RuntimeError, match="completed_owner_proposal_evidence"):
        OwnerCommandService._result_from_completed_proposal(_proposal(data))
