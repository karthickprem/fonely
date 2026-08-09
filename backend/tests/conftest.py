"""Shared test configuration."""

import os

import pytest

POSTGRES_AVAILABLE = bool(os.environ.get("FONELY_TEST_DATABASE_URL"))

postgres = pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="FONELY_TEST_DATABASE_URL not set — PostgreSQL tests skipped",
)


@pytest.fixture(autouse=True)
def record_canonical_node_id(request: pytest.FixtureRequest, record_property: object) -> None:
    """Embed the exact pytest node ID in JUnit for verifier matching."""
    record_property("node_id", request.node.nodeid)  # type: ignore[operator]
