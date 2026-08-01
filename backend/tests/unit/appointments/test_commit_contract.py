"""Pure contracts for the future caller-owned D3 transaction runner."""

import pytest
from pydantic import ValidationError

from fonely.domain.appointments.commit_contract import (
    APPOINTMENT_CREATE_POST_COMPLETION_CONSTRAINTS,
    APPOINTMENT_CREATE_PRE_COMPLETION_CONSTRAINTS,
    set_constraints_immediate_sql,
)
from fonely.domain.appointments.results import (
    AppointmentCommitFailureCode,
    PreCommitAppointmentFailure,
)


def test_each_expected_failure_code_has_fixed_retryability() -> None:
    for code in AppointmentCommitFailureCode:
        outcome = PreCommitAppointmentFailure(
            pending_action_id=7,
            pending_action_version=3,
            error_code=code,
        )
        assert outcome.outcome == "failure"
        assert outcome.retryable is True
        assert outcome.model_dump(mode="json")["retryable"] is True


def test_retryability_cannot_be_overridden_and_result_is_frozen() -> None:
    with pytest.raises(ValidationError):
        PreCommitAppointmentFailure(
            pending_action_id=7,
            pending_action_version=3,
            error_code=AppointmentCommitFailureCode.RESOURCE_UNAVAILABLE,
            retryable=False,
        )
    outcome = PreCommitAppointmentFailure(
        pending_action_id=7,
        pending_action_version=3,
        error_code=AppointmentCommitFailureCode.REVALIDATION_REQUIRED,
    )
    with pytest.raises(ValidationError):
        outcome.error_code = AppointmentCommitFailureCode.TRANSACTION_FAILED


def test_unknown_failure_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PreCommitAppointmentFailure(
            pending_action_id=7,
            pending_action_version=3,
            error_code="unexpected_exception",
        )


def test_exact_named_constraint_sql() -> None:
    assert set_constraints_immediate_sql(APPOINTMENT_CREATE_PRE_COMPLETION_CONSTRAINTS) == (
        "SET CONSTRAINTS "
        "ck_confirmed_appointment_active_allocation_from_appointment, "
        "ck_confirmed_appointment_active_allocation_from_allocation IMMEDIATE"
    )
    assert set_constraints_immediate_sql(APPOINTMENT_CREATE_POST_COMPLETION_CONSTRAINTS) == (
        "SET CONSTRAINTS ck_customer_conversation_appointment_provenance IMMEDIATE"
    )
    subset = (
        APPOINTMENT_CREATE_PRE_COMPLETION_CONSTRAINTS[1],
        APPOINTMENT_CREATE_POST_COMPLETION_CONSTRAINTS[0],
    )
    assert set_constraints_immediate_sql(subset) == (
        "SET CONSTRAINTS "
        "ck_confirmed_appointment_active_allocation_from_allocation, "
        "ck_customer_conversation_appointment_provenance IMMEDIATE"
    )


@pytest.mark.parametrize(
    "names",
    [
        (),
        (
            "ck_customer_conversation_appointment_provenance",
            "ck_customer_conversation_appointment_provenance",
        ),
        ("ALL",),
        ("ck_safe, ALL",),
        ("ck_unknown",),
        (" ck_customer_conversation_appointment_provenance",),
        ('"ck_customer_conversation_appointment_provenance"',),
        ("public.ck_customer_conversation_appointment_provenance",),
        ("ck_safe IMMEDIATE; DROP TABLE appointments; --",),
        ("ck_safe--comment",),
        ("ck_safe/*comment*/",),
        ("ck_safe IMMEDIATE",),
        ("CK_customer_conversation_appointment_provenance",),
    ],
)
def test_constraint_renderer_rejects_unapproved_or_unsafe_names(
    names: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        set_constraints_immediate_sql(names)
