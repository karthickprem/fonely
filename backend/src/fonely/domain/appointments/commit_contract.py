"""Pure D3 transaction-boundary constants; no repository or service implementation."""

import re

APPOINTMENT_CREATE_PRE_COMPLETION_CONSTRAINTS = (
    "ck_confirmed_appointment_active_allocation_from_appointment",
    "ck_confirmed_appointment_active_allocation_from_allocation",
)

APPOINTMENT_CREATE_POST_COMPLETION_CONSTRAINTS = (
    "ck_customer_conversation_appointment_provenance",
)

APPOINTMENT_CANCEL_PRE_COMPLETION_CONSTRAINTS = (
    "ck_confirmed_appointment_active_allocation_from_appointment",
    "ck_confirmed_appointment_active_allocation_from_allocation",
)

APPOINTMENT_CANCEL_POST_COMPLETION_CONSTRAINTS = (
    "ck_appointment_mutation_commit",
    "ck_appointment_commit_provenance",
    "ck_confirmed_appointment_action_commit",
)

APPOINTMENT_RESCHEDULE_PRE_COMPLETION_CONSTRAINTS = (
    "ck_confirmed_appointment_active_allocation_from_appointment",
    "ck_confirmed_appointment_active_allocation_from_allocation",
)

APPOINTMENT_RESCHEDULE_POST_COMPLETION_CONSTRAINTS = (
    "ck_appointment_mutation_commit",
    "ck_appointment_commit_provenance",
    "ck_confirmed_appointment_action_commit",
)

_APPROVED_CONSTRAINTS = frozenset(
    APPOINTMENT_CREATE_PRE_COMPLETION_CONSTRAINTS
    + APPOINTMENT_CREATE_POST_COMPLETION_CONSTRAINTS
    + APPOINTMENT_CANCEL_PRE_COMPLETION_CONSTRAINTS
    + APPOINTMENT_CANCEL_POST_COMPLETION_CONSTRAINTS
    + APPOINTMENT_RESCHEDULE_PRE_COMPLETION_CONSTRAINTS
    + APPOINTMENT_RESCHEDULE_POST_COMPLETION_CONSTRAINTS
)
_CONSTRAINT_IDENTIFIER = re.compile(r"^ck_[a-z][a-z0-9_]{1,62}$")


def set_constraints_immediate_sql(constraint_names: tuple[str, ...]) -> str:
    """Render one statement for an approved ordered subset of D3 constraints."""
    if not constraint_names:
        raise ValueError("At least one deferred constraint name is required")
    if len(set(constraint_names)) != len(constraint_names):
        raise ValueError("Deferred constraint names must be unique")
    for name in constraint_names:
        if _CONSTRAINT_IDENTIFIER.fullmatch(name) is None:
            raise ValueError("Invalid deferred constraint identifier")
        if name not in _APPROVED_CONSTRAINTS:
            raise ValueError("Deferred constraint is not approved for D3")
    return f"SET CONSTRAINTS {', '.join(constraint_names)} IMMEDIATE"
