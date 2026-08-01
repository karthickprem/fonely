"""Prevent internal lifecycle operations from future external registration."""

from fonely.services.exposure_policy import (
    EXTERNAL_PENDING_ACTION_OPERATIONS,
    INTERNAL_PENDING_ACTION_OPERATIONS,
)

INTERNAL_REQUIRED = {
    "begin_commit",
    "complete_commit",
    "fail_commit",
    "internal_get",
    "internal_get_active",
}


def test_internal_operations_are_never_external() -> None:
    assert INTERNAL_REQUIRED <= INTERNAL_PENDING_ACTION_OPERATIONS
    assert INTERNAL_REQUIRED.isdisjoint(EXTERNAL_PENDING_ACTION_OPERATIONS)


def test_external_and_internal_registries_are_disjoint() -> None:
    assert EXTERNAL_PENDING_ACTION_OPERATIONS.isdisjoint(INTERNAL_PENDING_ACTION_OPERATIONS)
