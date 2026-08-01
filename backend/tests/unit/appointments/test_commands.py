"""Strict appointment command and result boundaries."""

from datetime import UTC, date, datetime, time, timedelta, timezone

import pytest
from pydantic import ValidationError

from fonely.domain.appointments.commands import (
    BlockResourceTimeCommand,
    CheckAvailabilityQuery,
    ConfirmPendingAppointmentCommand,
    CreatePendingAppointmentCommand,
)
from fonely.domain.appointments.results import (
    AppointmentConfirmationResult,
    AppointmentLookupResult,
    AppointmentProposalResult,
    AvailabilitySlot,
    ResourceBlockResult,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole

NOW = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)

VALID_FACTS = {
    "operation": "create",
    "service_id": 1,
    "service_name": "Haircut",
    "resource_id": 1,
    "resource_name": "Priya",
    "start_at": "2026-08-03T10:00:00Z",
    "end_at": "2026-08-03T10:30:00Z",
    "duration_minutes": 30,
    "price": "500.00",
    "business_timezone": "Asia/Kolkata",
}


def actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        session_id="session",
    )


def test_create_command_is_strict_and_rejects_role_elevation() -> None:
    with pytest.raises(ValidationError):
        CreatePendingAppointmentCommand(
            actor=actor(),
            service_id=1,
            resource_id=2,
            start_at=NOW,
            customer_name=None,
            customer_phone="+919123456789",
            reason=None,
            call_id=None,
            expires_at=NOW,
            idempotency_key="key",
            verified_role="owner",  # type: ignore[call-arg]
        )


def test_confirmation_contains_only_action_identity_and_version() -> None:
    command = ConfirmPendingAppointmentCommand(
        actor=actor(), pending_action_id=1, expected_version=2
    )
    assert command.model_dump().keys() == {"actor", "pending_action_id", "expected_version"}


@pytest.mark.parametrize("field", ["pending_action_id", "expected_version"])
def test_command_integer_fields_use_postgresql_bounds(field: str) -> None:
    values = {"actor": actor(), "pending_action_id": 2_147_483_647, "expected_version": 1}
    ConfirmPendingAppointmentCommand(**values)

    values[field] = 2_147_483_648
    with pytest.raises(ValidationError):
        ConfirmPendingAppointmentCommand(**values)


@pytest.mark.parametrize("field", ["pending_action_id", "version"])
def test_result_integer_fields_use_postgresql_bounds(field: str) -> None:
    values: dict[str, object] = {
        "pending_action_id": 2_147_483_647,
        "version": 2_147_483_647,
        "expires_at": NOW,
        "confirmation_facts": VALID_FACTS,
    }
    AppointmentProposalResult(**values)  # type: ignore[arg-type]

    values[field] = 2_147_483_648
    with pytest.raises(ValidationError):
        AppointmentProposalResult(**values)  # type: ignore[arg-type]


def test_block_command_rejects_naive_time() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        BlockResourceTimeCommand(
            actor=actor(),
            resource_id=1,
            effective_start_at=datetime(2026, 8, 1, 10),
            effective_end_at=NOW,
            reason=None,
            idempotency_key="block",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pending_action_id", "1"),
        ("version", "2"),
        ("expires_at", "2026-08-01T08:00:00Z"),
    ],
)
def test_result_models_reject_coercible_values(field: str, value: object) -> None:
    values: dict[str, object] = {
        "pending_action_id": 1,
        "version": 2,
        "expires_at": NOW,
        "confirmation_facts": {},
    }
    values[field] = value
    with pytest.raises(ValidationError):
        AppointmentProposalResult(**values)  # type: ignore[arg-type]


def test_result_models_reject_unknown_fields_and_are_immutable() -> None:
    with pytest.raises(ValidationError):
        AppointmentProposalResult(
            pending_action_id=1,
            version=2,
            expires_at=NOW,
            confirmation_facts=VALID_FACTS,
            appointment_id=3,  # type: ignore[call-arg]
        )
    result = AppointmentProposalResult(
        pending_action_id=1, version=2, expires_at=NOW, confirmation_facts=VALID_FACTS
    )
    with pytest.raises(ValidationError):
        result.version = 3


def test_proposal_confirmation_facts_serialize_and_immutable() -> None:
    result = AppointmentProposalResult(
        pending_action_id=1, version=2, expires_at=NOW, confirmation_facts=VALID_FACTS
    )
    dumped = result.model_dump(mode="json")
    assert dumped["confirmation_facts"]["service_id"] == 1
    assert dumped["confirmation_facts"]["operation"] == "create"
    json_str = result.model_dump_json()
    assert "Haircut" in json_str
    roundtrip = AppointmentProposalResult.model_validate(result.model_dump())
    assert roundtrip.confirmation_facts.service_id == 1
    with pytest.raises(ValidationError):
        result.confirmation_facts = VALID_FACTS  # type: ignore[misc]
    with pytest.raises(ValidationError):
        result.confirmation_facts.service_id = 99  # type: ignore[misc]


def test_proposal_states_that_slot_is_not_held() -> None:
    result = AppointmentProposalResult(
        pending_action_id=1,
        version=2,
        expires_at=NOW,
        confirmation_facts=VALID_FACTS,
    )
    assert result.status == "awaiting_confirmation"
    assert result.slot_is_held is False


def test_availability_rejects_invalid_or_aware_wall_time_range() -> None:
    base = {
        "actor": actor(),
        "service_id": 1,
        "local_date": date(2026, 8, 2),
    }
    with pytest.raises(ValidationError, match="after earliest"):
        CheckAvailabilityQuery(
            **base,
            earliest_local_time=time(12),
            latest_local_time=time(11),
        )
    with pytest.raises(ValidationError, match="naive local wall times"):
        CheckAvailabilityQuery(
            **base,
            earliest_local_time=time(10, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (NOW, NOW),
        (NOW, NOW - timedelta(minutes=1)),
        (NOW, datetime(2026, 8, 1, 13, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))),
    ],
)
def test_block_command_requires_positive_instant_interval(start: datetime, end: datetime) -> None:
    with pytest.raises(ValidationError, match="end must be after start"):
        BlockResourceTimeCommand(
            actor=actor(),
            resource_id=1,
            effective_start_at=start,
            effective_end_at=end,
            reason=None,
            idempotency_key="block",
        )


def test_block_command_preserves_microsecond_precision_near_datetime_max() -> None:
    end = datetime.max.replace(tzinfo=UTC)
    command = BlockResourceTimeCommand(
        actor=actor(),
        resource_id=1,
        effective_start_at=end - timedelta(microseconds=1),
        effective_end_at=end,
        reason=None,
        idempotency_key="block-max",
    )

    assert command.effective_end_at > command.effective_start_at


def test_actor_context_and_nested_result_facts_are_immutable() -> None:
    context = actor()
    with pytest.raises(ValidationError):
        context.business_id = 2

    result = AppointmentProposalResult(
        pending_action_id=1,
        version=2,
        expires_at=NOW,
        confirmation_facts=VALID_FACTS,
    )
    with pytest.raises(ValidationError):
        result.confirmation_facts.service_name = "Changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        result.confirmation_facts = VALID_FACTS  # type: ignore[misc]
    with pytest.raises(ValidationError):
        AppointmentProposalResult(
            pending_action_id=1,
            version=2,
            expires_at=NOW,
            confirmation_facts={"arbitrary": "object"},
        )


def test_result_timestamp_must_be_aware() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        AppointmentProposalResult(
            pending_action_id=1,
            version=2,
            expires_at=datetime(2026, 8, 1, 8),
            confirmation_facts=VALID_FACTS,
        )


@pytest.mark.parametrize(
    "timezone_name", ["Mars/Salon", "localtime", "Factory", "posixrules", "posix/UTC", "right/UTC"]
)
def test_availability_slot_rejects_invalid_business_timezone(timezone_name: str) -> None:
    with pytest.raises(ValidationError, match="Invalid timezone"):
        AvailabilitySlot(
            service_id=1,
            service_name="Haircut",
            resource_id=2,
            resource_name="Priya",
            start_at=NOW,
            end_at=NOW + timedelta(minutes=30),
            effective_start_at=NOW,
            effective_end_at=NOW + timedelta(minutes=30),
            duration_minutes=30,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
            business_timezone=timezone_name,
        )


def test_confirmation_result_accepts_canonical_business_timezone() -> None:
    result = AppointmentConfirmationResult(
        appointment_id=1,
        pending_action_id=2,
        service_id=3,
        service_name="Haircut",
        resource_id=4,
        resource_name="Priya",
        start_at=NOW,
        end_at=NOW + timedelta(minutes=30),
        price=None,
        business_timezone="Asia/Kolkata",
    )
    assert result.business_timezone == "Asia/Kolkata"


@pytest.mark.parametrize(
    "timezone_name", ["Mars/Salon", "localtime", "Factory", "posixrules", "posix/UTC", "right/UTC"]
)
def test_confirmation_result_rejects_invalid_business_timezone(timezone_name: str) -> None:
    with pytest.raises(ValidationError, match="Invalid timezone"):
        AppointmentConfirmationResult(
            appointment_id=1,
            pending_action_id=2,
            service_id=3,
            service_name="Haircut",
            resource_id=4,
            resource_name="Priya",
            start_at=NOW,
            end_at=NOW + timedelta(minutes=30),
            price=None,
            business_timezone=timezone_name,
        )


def valid_availability_slot() -> dict[str, object]:
    return {
        "service_id": 1,
        "service_name": "Haircut",
        "resource_id": 2,
        "resource_name": "Priya",
        "start_at": NOW,
        "end_at": NOW + timedelta(minutes=30),
        "effective_start_at": NOW - timedelta(minutes=5),
        "effective_end_at": NOW + timedelta(minutes=40),
        "duration_minutes": 30,
        "buffer_before_minutes": 5,
        "buffer_after_minutes": 10,
        "business_timezone": "Asia/Kolkata",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("service_id", 0),
        ("resource_id", -1),
        ("duration_minutes", 0),
        ("duration_minutes", 721),
        ("buffer_before_minutes", -1),
        ("buffer_after_minutes", 241),
        ("end_at", NOW),
        ("effective_start_at", NOW),
        ("effective_end_at", NOW + timedelta(minutes=30)),
    ],
)
def test_availability_slot_rejects_malformed_facts(field: str, value: object) -> None:
    values = valid_availability_slot()
    values[field] = value
    with pytest.raises(ValidationError):
        AvailabilitySlot(**values)  # type: ignore[arg-type]


def test_lookup_facts_are_non_null_and_intervals_are_ordered() -> None:
    values: dict[str, object] = {
        "appointment_id": 1,
        "version": 1,
        "customer_phone": "+919123456789",
        "service_id": 2,
        "service_name": "Haircut",
        "resource_id": 3,
        "resource_name": "Priya",
        "start_at": NOW,
        "end_at": NOW + timedelta(minutes=30),
        "status": "confirmed",
    }
    AppointmentLookupResult(**values)  # type: ignore[arg-type]
    for field in ("service_id", "service_name", "resource_name"):
        invalid = values.copy()
        invalid[field] = None
        with pytest.raises(ValidationError):
            AppointmentLookupResult(**invalid)  # type: ignore[arg-type]


def test_resource_block_result_requires_positive_ordered_interval() -> None:
    with pytest.raises(ValidationError):
        ResourceBlockResult(
            allocation_id=0,
            resource_id=1,
            effective_start_at=NOW,
            effective_end_at=NOW,
            status="active",
        )
