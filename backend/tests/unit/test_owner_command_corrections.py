"""Regression tests for trusted owner intent and bounded confirmation semantics."""

import inspect
from datetime import date, datetime, time
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from fonely.domain.appointments.availability import LocalShift
from fonely.services.owner_command_parser import ParsedOwnerCommand
from fonely.services.owner_commands import OwnerCommandService


def _service() -> OwnerCommandService:
    service = OwnerCommandService(AsyncMock(), AsyncMock())
    service._proposals = AsyncMock()
    service._appointments = AsyncMock()
    service._get_business_timezone = AsyncMock(return_value="Asia/Kolkata")
    return service


def test_process_command_has_no_caller_owner_identity_override() -> None:
    parameters = inspect.signature(OwnerCommandService.process_command).parameters
    assert "owner_user_id" not in parameters

    service = _service()
    with pytest.raises(TypeError):
        service.process_command(  # type: ignore[call-arg]
            1,
            "+919000000001",
            "YES",
            owner_user_id=999,
        )


async def test_unsupported_tamil_date_returns_no_date() -> None:
    service = _service()
    assert await service._resolve_date(1, "அடுத்த நல்ல நாள்") is None


async def test_ambiguous_partial_resource_match_returns_none() -> None:
    service = _service()
    first = MagicMock(id=1)
    first.name = "Dr. Priya Raman"
    second = MagicMock(id=2)
    second.name = "Dr. Priya Kumar"
    result = MagicMock()
    result.scalars.return_value.all.return_value = [first, second]
    service._session.execute = AsyncMock(return_value=result)

    assert await service._resolve_resource(1, "Priya") is None


async def test_invalid_date_creates_no_proposal() -> None:
    service = _service()
    owner = MagicMock(id=1, phone="+919000000001")
    parsed = ParsedOwnerCommand(command="close_clinic", date="அடுத்த நல்ல நாள்", reason="Holiday")

    result = await service._create_preview(1, owner, parsed)

    assert result.success is False
    service._proposals.create_idempotent.assert_not_awaited()


async def test_revised_pending_intent_never_renders_new_snapshot() -> None:
    service = _service()
    owner = MagicMock(id=1, phone="+919000000001")
    old = MagicMock(
        id="old-proposal",
        idempotency_key="old-key",
        payload_digest="old-digest",
        status="pending_confirmation",
    )
    service._proposals.get_by_idempotency_key.return_value = None
    service._proposals.create_idempotent.return_value = None
    service._proposals.get_latest_pending_for_owner.return_value = old
    service._validate_close_early = AsyncMock(
        return_value=(
            time(15, 0),
            [LocalShift(time(9, 0), time(15, 0))],
            [],
        )
    )
    parsed = ParsedOwnerCommand(
        command="close_early",
        date=date.today().isoformat(),
        close_time="15:00",
        reason="Emergency",
    )

    result = await service._create_preview(1, owner, parsed)

    assert result.success is False
    assert result.proposal_id == "old-proposal"
    assert "15:00" not in result.response_text
    assert "different command is pending" in result.response_text.lower()


async def test_bare_yes_does_not_replay_historical_completed_proposal() -> None:
    service = _service()
    owner = MagicMock(id=1, phone="+919000000001")
    service._require_active_owner = AsyncMock(return_value=owner)
    service._proposals.get_latest_pending_for_owner.return_value = None
    service._proposals.get_latest_for_owner.return_value = MagicMock(
        status="completed", id="historical"
    )
    service._parser.parse = AsyncMock()

    result = await service.process_command(1, owner.phone, "YES")

    assert result.success is False
    assert "no current command" in result.response_text.lower()
    service._parser.parse.assert_not_awaited()
    service._proposals.get_latest_for_owner.assert_not_awaited()


async def test_terminal_stale_intent_allows_corrected_new_proposal() -> None:
    service = _service()
    owner = MagicMock(id=1, phone="+919000000001")
    proposal = MagicMock(
        id="corrected",
        preview_snapshot={
            "command_type": "close_early",
            "resolved_date": date.today().isoformat(),
            "clinic_timezone": "Asia/Kolkata",
            "resource_id": None,
            "resource_name": None,
            "schedule_mutation": {
                "type": "schedule_exception",
                "is_closed": False,
                "open_time": "09:00:00",
                "close_time": "15:00:00",
                "reason": "Corrected",
            },
            "close_time": "15:00:00",
            "appointments": [],
        },
        idempotency_key="corrected-key",
        payload_digest="a" * 64,
        status="pending_confirmation",
    )
    service._proposals.get_by_idempotency_key.return_value = MagicMock(
        status="failed", payload_digest="old", idempotency_key="old"
    )
    service._proposals.create_idempotent.return_value = proposal
    service._validate_close_early = AsyncMock(
        return_value=(
            time(15, 0),
            [LocalShift(time(9, 0), time(15, 0))],
            [],
        )
    )
    parsed = ParsedOwnerCommand(
        command="close_early",
        date=date.today().isoformat(),
        close_time="15:00",
        reason="Corrected",
    )

    result = await service._create_preview(1, owner, parsed)

    assert result.success is True
    assert result.proposal_id == "corrected"
    service._proposals.create_idempotent.assert_awaited_once()


async def test_close_early_targeting_uses_proposed_truncated_window() -> None:
    service = _service()
    service._appointments.list_active_resource_ids.return_value = (1,)
    appointment = MagicMock(
        id=5,
        start_at=MagicMock(),
        effective_start_at=None,
        end_at=None,
        effective_end_at=None,
    )
    zone = ZoneInfo("Asia/Kolkata")
    start = datetime.combine(date.today(), time(16, 0), zone)
    appointment.start_at = start
    appointment.end_at = start.replace(hour=16, minute=30)
    appointment.effective_start_at = appointment.start_at
    appointment.effective_end_at = appointment.end_at
    appointment.version = 1
    appointment.resource_id = 1
    appointment.service_id = 1
    appointment.service_name_snapshot = "Consultation"
    appointment.resource_name_snapshot = "Doctor"
    appointment.customer_name = "Patient"
    result = MagicMock()
    result.scalars.return_value.all.return_value = [appointment]
    service._session.execute = AsyncMock(return_value=result)

    targets = await service._appointments_outside_schedule(
        1,
        date.today(),
        "Asia/Kolkata",
        (LocalShift(time(9, 0), time(15, 0)),),
    )

    assert [target["appointment_id"] for target in targets] == [5]
