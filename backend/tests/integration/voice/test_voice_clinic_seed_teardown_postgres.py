"""Safety proofs for the voice_clinic_seed fixture's ownership-aware teardown.

The teardown suppresses immutability triggers with ``SET LOCAL
session_replication_role = replica`` so it can reclaim the test's own
resource_allocations/appointment_commits rows. These tests prove that relaxation
is strictly transaction-local and fail-closed:

  * after a normal teardown, a FRESH session sees session_replication_role='origin'
    (no pooled-connection contamination);
  * a mid-teardown error ROLLS BACK, still restores 'origin' on the reused pooled
    connection, and re-raises loudly (cleanup never silently half-completes);
  * the deterministic clinic + booking rows are actually gone (guarded tables at
    zero) so the migration downgrade guards are not tripped.

Requires a Postgres role with the session_replication_role privilege (the CI
Postgres container user is superuser). Test-only; the helper is never exposed
outside this conftest.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.integration.voice import conftest as seed_conftest

pytestmark = pytest.mark.postgres


async def _replication_role() -> str:
    from fonely.core.database import async_session

    async with async_session() as s:
        return await s.scalar(text("SELECT current_setting('session_replication_role')"))


async def test_normal_teardown_restores_origin_and_clears_business(voice_clinic_seed) -> None:
    # The fixture has seeded business 1. Nothing else to do here — the assertions
    # below run AFTER this test's teardown, so we validate state in a follow-up
    # session created by the fixture finalizer having already run is not possible
    # inline; instead prove the invariant directly by invoking teardown here and
    # confirming a fresh session sees 'origin'.
    from fonely.core.database import async_session

    # Directly exercise the teardown helper, then confirm a FRESH pooled session
    # sees the setting back at origin (SET LOCAL auto-reverted on commit).
    async with async_session() as s:
        await seed_conftest._teardown_voice_clinic(s)
    assert await _replication_role() == "origin"
    # Guarded tables are empty, so a downgrade guard would not trip.
    from sqlalchemy import text as _t

    async with async_session() as s:
        for tbl in ("business_whatsapp_channels", "calls", "resource_allocations"):
            n = await s.scalar(_t(f"SELECT count(*) FROM {tbl}"))
            assert n == 0, f"{tbl} not cleared by teardown"


async def test_midteardown_error_rolls_back_and_restores_origin() -> None:
    # Force an error PART-WAY through the teardown (after SET LOCAL replica) and
    # prove: it re-raises loudly, and a fresh session still sees 'origin' — the
    # relaxed replication role never leaks onto the reused pooled connection.
    from fonely.core.database import async_session

    # Seed a clean business 1 to have rows to delete.
    async with async_session() as s:
        await seed_conftest._assert_business_absent(s)
        await seed_conftest._seed_voice_clinic(s)

    original = seed_conftest._TEARDOWN_STATEMENTS
    # Inject a statement that errors AFTER SET LOCAL replica is in effect.
    seed_conftest._TEARDOWN_STATEMENTS = (
        "DELETE FROM resource_allocations WHERE appointment_id IN "
        "(SELECT id FROM appointments WHERE business_id = :bid)",
        "SELECT * FROM table_that_does_not_exist_forcing_error",
    )
    try:
        # The injected bad statement raises a DBAPIError (undefined table),
        # which the teardown re-raises after rolling back.
        with pytest.raises(DBAPIError):
            async with async_session() as s:
                await seed_conftest._teardown_voice_clinic(s)
    finally:
        seed_conftest._TEARDOWN_STATEMENTS = original

    # The relaxed setting must NOT survive the rolled-back transaction.
    assert await _replication_role() == "origin"

    # Clean up the seeded business for real so this test leaves no residue.
    async with async_session() as s:
        await seed_conftest._teardown_voice_clinic(s)
    async with async_session() as s:
        assert await s.scalar(text("SELECT count(*) FROM business_whatsapp_channels")) == 0
