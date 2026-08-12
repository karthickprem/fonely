"""Database-backed provider channel identity resolution.

Replaces EXOTEL_NUMBER_MAPPINGS as the authority for which dialed number
belongs to which tenant, for any provider rather than for Exotel specifically.

Every method fails closed. An unknown or disabled identifier resolves to None
and the caller must refuse the call; none of them fall back to a default
tenant, because a default tenant is precisely how one clinic's patient ends up
booked into another clinic's diary.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("fonely.repositories.channel_identities")

# The provider key for Exotel telephony. A constant rather than a bare string
# at each call site so a typo becomes an import error instead of a number that
# silently resolves to no tenant.
PROVIDER_EXOTEL = "exotel"


class ChannelIdentityRepository:
    """Read-side resolution of tenant <-> provider channel identity."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_business_id(self, provider: str, external_identifier: str) -> int | None:
        """Return the tenant reachable on this identifier, or None.

        The database holds a global uniqueness constraint on
        (provider, external_identifier), so this can never be ambiguous. A
        disabled identity returns None: a decommissioned number must stop
        accepting patient calls rather than keep writing into the clinic's
        records.
        """
        if not provider or not external_identifier:
            return None
        result = await self._session.execute(
            text(
                "SELECT business_id FROM business_channel_identities "
                "WHERE provider = :provider "
                "  AND external_identifier = :identifier "
                "  AND status = 'active'"
            ),
            {"provider": provider, "identifier": external_identifier},
        )
        row = result.one_or_none()
        return None if row is None else int(row[0])

    async def resolve_identifier(self, business_id: int, provider: str) -> str | None:
        """Return the identifier a business is reached on for a provider.

        Preference order: the active primary, else the sole active identity.
        A business with several active identities and no designated primary is
        ambiguous and resolves to None rather than picking by row order. The
        registration path always designates a primary, so that state is only
        reachable by direct SQL — but it must not silently pick one.
        """
        result = await self._session.execute(
            text(
                "SELECT external_identifier, is_primary "
                "FROM business_channel_identities "
                "WHERE business_id = :bid AND provider = :provider "
                "  AND status = 'active' "
                "ORDER BY is_primary DESC, id ASC"
            ),
            {"bid": business_id, "provider": provider},
        )
        rows = result.all()
        if not rows:
            return None
        if rows[0][1] or len(rows) == 1:
            return str(rows[0][0])
        logger.error(
            "channel_identity_ambiguous",
            extra={
                "business_id": business_id,
                "provider": provider,
                "active_identities": len(rows),
            },
        )
        return None


class ChannelIdentityConflictError(Exception):
    """The identifier is already attached to a different tenant."""


async def register_channel_identity(
    session: AsyncSession,
    *,
    business_id: int,
    provider: str,
    external_identifier: str,
    label: str | None = None,
    make_primary: bool = True,
) -> None:
    """Attach a provider identifier to a business. Caller owns the transaction.

    Raises ChannelIdentityConflictError if the identifier already belongs to
    another tenant. Re-pointing a live number is a deliberate operator action
    with patient-visible consequences — calls start landing somewhere else —
    so it must not happen as a side effect of onboarding, and must not
    silently do nothing either.
    """
    # Fast path only. This SELECT cannot make the check safe under concurrency:
    # FOR UPDATE locks rows that exist, and Postgres takes no gap lock for a row
    # that does not, so two tenants registering the same identifier at the same
    # instant both read NULL here and both proceed. The authoritative check is
    # the ON CONFLICT ... WHERE below, which runs under the unique index lock.
    # This one survives because it produces a clear error before we mutate any
    # of the caller's own rows in the demote step.
    existing = await session.execute(
        text(
            "SELECT business_id FROM business_channel_identities "
            "WHERE provider = :provider AND external_identifier = :identifier "
            "FOR UPDATE"
        ),
        {"provider": provider, "identifier": external_identifier},
    )
    owner_row = existing.one_or_none()
    if owner_row is not None and int(owner_row[0]) != business_id:
        raise ChannelIdentityConflictError(
            f"{provider} identifier is already registered to business_id={owner_row[0]}"
        )

    if make_primary:
        # Demote first: the partial unique index permits only one active
        # primary per (business, provider), so promoting without demoting
        # would abort the transaction.
        await session.execute(
            text(
                "UPDATE business_channel_identities SET is_primary = false, "
                "updated_at = NOW() "
                "WHERE business_id = :bid AND provider = :provider AND is_primary"
            ),
            {"bid": business_id, "provider": provider},
        )

    # The DO UPDATE is deliberately guarded on the row already belonging to this
    # tenant, and deliberately never assigns business_id. Without the guard, a
    # tenant that loses the race updates the WINNER's row: business_id stays with
    # the winner while label/is_primary are overwritten, the statement succeeds,
    # and the loser is told 201 Created for a number that routes to somebody
    # else. A caller cannot tell that apart from a real registration, and the
    # people who would discover it are the patients whose calls land in the
    # wrong clinic. When the guard rejects the update no row comes back, and
    # that is the authoritative conflict signal — it is evaluated while this
    # statement holds the unique index lock, so it is race-free in a way the
    # SELECT above can never be.
    upserted = await session.execute(
        text(
            "INSERT INTO business_channel_identities "
            "(business_id, provider, external_identifier, label, "
            " status, is_primary, created_at, updated_at) "
            "VALUES (:bid, :provider, :identifier, :label, 'active', "
            " :primary, NOW(), NOW()) "
            "ON CONFLICT (provider, external_identifier) DO UPDATE SET "
            "  label = EXCLUDED.label, "
            "  status = 'active', "
            "  is_primary = EXCLUDED.is_primary, "
            "  updated_at = NOW() "
            "WHERE business_channel_identities.business_id = EXCLUDED.business_id "
            "RETURNING business_id"
        ),
        {
            "bid": business_id,
            "provider": provider,
            "identifier": external_identifier,
            "label": label,
            "primary": make_primary,
        },
    )
    if upserted.one_or_none() is None:
        raise ChannelIdentityConflictError(
            f"{provider} identifier {external_identifier!r} was registered to another "
            "business concurrently"
        )
    await session.flush()
