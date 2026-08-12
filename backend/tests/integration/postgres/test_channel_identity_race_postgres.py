"""Concurrent registration of the same dialed number by two tenants.

The pre-check in register_channel_identity is a SELECT ... FOR UPDATE, which
locks rows that exist. Postgres takes no gap lock for a row that does not, so
two tenants registering the same identifier in overlapping transactions both
read NULL and both proceed to the upsert.

What made that dangerous is that the ON CONFLICT branch never assigns
business_id: the loser's UPDATE would leave ownership with the winner while
overwriting label/is_primary, and return success. The loser is told its
signboard number is registered when the number routes to another clinic —
and patients are the ones who would find out.

These tests are deterministic rather than timing-dependent: the loser's INSERT
blocks on the unique index until the winner commits, so the interleaving is
forced by the database, not by a sleep racing the scheduler.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.repositories.channel_identities import (
    ChannelIdentityConflictError,
    register_channel_identity,
)

pytestmark = pytest.mark.postgres

PROVIDER = "exotel"
NUMBER = "+918045551234"


async def _seed_two_businesses(session: AsyncSession) -> None:
    for bid, name in ((1, "Smile Dental Clinic"), (2, "Bright Dental Care")):
        await session.execute(
            text(
                "INSERT INTO businesses "
                "(id, name, category, primary_contact_phone, timezone, subscription, "
                "appointment_slot_interval_minutes) "
                f"VALUES ({bid}, '{name}', 'dental', '+91900000000{bid}', "
                "'Asia/Kolkata', 'trial', 30)"
            )
        )
    await session.commit()


async def _owner_of(factory: async_sessionmaker[AsyncSession]) -> int | None:
    async with factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT business_id FROM business_channel_identities "
                    "WHERE provider = :p AND external_identifier = :i"
                ),
                {"p": PROVIDER, "i": NUMBER},
            )
        ).one_or_none()
    return None if row is None else int(row[0])


async def _row_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        return int(
            (
                await s.execute(
                    text(
                        "SELECT count(*) FROM business_channel_identities "
                        "WHERE provider = :p AND external_identifier = :i"
                    ),
                    {"p": PROVIDER, "i": NUMBER},
                )
            ).scalar_one()
        )


async def test_concurrent_registration_does_not_silently_steal_ownership(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Business 2 registers while business 1's transaction is still open.

    Business 2 must be refused. The decisive assertion is row-level: ownership
    stays with business 1. A handler that returned a tidy response while the row
    moved would pass a transcript-level check and fail this one.
    """
    async with pg_session_factory() as setup:
        await _seed_two_businesses(setup)

    winner = pg_session_factory()
    loser = pg_session_factory()
    try:
        # Business 1 claims the number but has NOT committed.
        await register_channel_identity(
            winner,
            business_id=1,
            provider=PROVIDER,
            external_identifier=NUMBER,
            label="signboard",
        )

        # Business 2 attempts the same number concurrently. Its pre-check sees
        # nothing (business 1 is uncommitted), so it reaches the upsert and
        # blocks there on the unique index.
        loser_task = asyncio.create_task(
            register_channel_identity(
                loser,
                business_id=2,
                provider=PROVIDER,
                external_identifier=NUMBER,
                label="stolen",
            )
        )
        await asyncio.sleep(0.3)
        assert not loser_task.done(), (
            "business 2 completed before business 1 committed — it cannot have "
            "contended for the unique index, so this test proved nothing"
        )

        await winner.commit()

        with pytest.raises(ChannelIdentityConflictError):
            await loser_task
        await loser.rollback()
    finally:
        await winner.close()
        await loser.close()

    assert await _owner_of(pg_session_factory) == 1, (
        "the losing tenant took ownership of a number it was refused"
    )
    assert await _row_count(pg_session_factory) == 1


async def test_losing_tenant_does_not_overwrite_the_owners_label(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ownership is not the only field at stake.

    The unguarded ON CONFLICT would keep business_id but still apply the loser's
    label and is_primary to the winner's row — a partial write that leaves the
    number owned correctly and described wrongly.
    """
    async with pg_session_factory() as setup:
        await _seed_two_businesses(setup)

    winner = pg_session_factory()
    loser = pg_session_factory()
    try:
        await register_channel_identity(
            winner,
            business_id=1,
            provider=PROVIDER,
            external_identifier=NUMBER,
            label="reception-line",
        )
        loser_task = asyncio.create_task(
            register_channel_identity(
                loser,
                business_id=2,
                provider=PROVIDER,
                external_identifier=NUMBER,
                label="stolen",
            )
        )
        await asyncio.sleep(0.3)
        await winner.commit()
        with pytest.raises(ChannelIdentityConflictError):
            await loser_task
        await loser.rollback()
    finally:
        await winner.close()
        await loser.close()

    async with pg_session_factory() as s:
        label = (
            await s.execute(
                text(
                    "SELECT label FROM business_channel_identities "
                    "WHERE provider = :p AND external_identifier = :i"
                ),
                {"p": PROVIDER, "i": NUMBER},
            )
        ).scalar_one()
    assert label == "reception-line", f"the refused tenant overwrote the owner's label: {label!r}"


async def test_same_tenant_reregistration_is_still_idempotent(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The guard must not break the legitimate case it sits next to.

    Re-registering a number you already own updates the label and stays one row.
    A conflict check that also refused this would be a regression dressed as a
    fix.
    """
    async with pg_session_factory() as setup:
        await _seed_two_businesses(setup)

    async with pg_session_factory() as s:
        await register_channel_identity(
            s, business_id=1, provider=PROVIDER, external_identifier=NUMBER, label="old"
        )
        await s.commit()
    async with pg_session_factory() as s:
        await register_channel_identity(
            s, business_id=1, provider=PROVIDER, external_identifier=NUMBER, label="new"
        )
        await s.commit()

    assert await _owner_of(pg_session_factory) == 1
    assert await _row_count(pg_session_factory) == 1
    async with pg_session_factory() as s:
        label = (
            await s.execute(
                text(
                    "SELECT label FROM business_channel_identities "
                    "WHERE provider = :p AND external_identifier = :i"
                ),
                {"p": PROVIDER, "i": NUMBER},
            )
        ).scalar_one()
    assert label == "new"
