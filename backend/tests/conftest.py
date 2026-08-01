"""Shared test configuration."""

import os

import pytest

POSTGRES_AVAILABLE = bool(os.environ.get("FONELY_TEST_DATABASE_URL"))

postgres = pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="FONELY_TEST_DATABASE_URL not set — PostgreSQL tests skipped",
)
