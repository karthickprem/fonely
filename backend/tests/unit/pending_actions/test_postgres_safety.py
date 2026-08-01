"""Destructive PostgreSQL test-database guard tests."""

import pytest
from _pytest.outcomes import Failed

from tests.integration.postgres.conftest import _test_database_url

VALID_URL = "postgresql+asyncpg://fonely_test_user:secret@localhost:5432/fonely_test_run1"


def test_valid_explicit_test_database_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FONELY_TEST_DATABASE_URL", VALID_URL)
    monkeypatch.setenv("FONELY_ALLOW_DESTRUCTIVE_TEST_DB", "1")
    assert _test_database_url() == VALID_URL


def test_explicit_destructive_opt_in_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FONELY_TEST_DATABASE_URL", VALID_URL)
    monkeypatch.delenv("FONELY_ALLOW_DESTRUCTIVE_TEST_DB", raising=False)
    with pytest.raises(Failed, match="FONELY_ALLOW_DESTRUCTIVE_TEST_DB"):
        _test_database_url()


@pytest.mark.parametrize(
    "database_name",
    ["contest_production", "latest", "test_backup_of_production", "fonely_prod"],
)
def test_loose_or_production_like_database_names_rejected(
    monkeypatch: pytest.MonkeyPatch,
    database_name: str,
) -> None:
    monkeypatch.setenv(
        "FONELY_TEST_DATABASE_URL",
        f"postgresql+asyncpg://fonely_test_user:secret@localhost:5432/{database_name}",
    )
    monkeypatch.setenv("FONELY_ALLOW_DESTRUCTIVE_TEST_DB", "1")
    with pytest.raises(Failed, match="Database name"):
        _test_database_url()


def test_dedicated_test_role_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FONELY_TEST_DATABASE_URL",
        "postgresql+asyncpg://app_user:secret@localhost:5432/fonely_test",
    )
    monkeypatch.setenv("FONELY_ALLOW_DESTRUCTIVE_TEST_DB", "1")
    with pytest.raises(Failed, match="dedicated test role"):
        _test_database_url()


@pytest.mark.parametrize("hostname", ["prod-db", "staging-db", "db.example.com"])
def test_non_local_host_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    hostname: str,
) -> None:
    monkeypatch.setenv(
        "FONELY_TEST_DATABASE_URL",
        f"postgresql+asyncpg://fonely_test_user:secret@{hostname}:5432/fonely_test",
    )
    monkeypatch.setenv("FONELY_ALLOW_DESTRUCTIVE_TEST_DB", "1")
    with pytest.raises(Failed, match="local database host"):
        _test_database_url()
