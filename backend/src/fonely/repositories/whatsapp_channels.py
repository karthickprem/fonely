"""Database-backed WhatsApp channel identity resolution.

Replaces the WHATSAPP_BUSINESS_MAPPINGS environment variable as the authority
for which provider number belongs to which tenant.

Every method here fails closed. An unknown, disabled, or ambiguous channel
resolves to None and the caller must refuse the operation; none of them fall
back to a process-wide default, because a process-wide default is precisely
how one tenant's message ends up sent from another tenant's number.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("fonely.repositories.whatsapp_channels")


class WhatsAppChannelRepository:
    """Read-side resolution of tenant <-> provider channel identity."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_business_id(self, phone_number_id: str) -> int | None:
        """Return the tenant that owns an inbound number, or None.

        The database holds a global uniqueness constraint on phone_number_id,
        so this can never be ambiguous. A disabled channel returns None: a
        decommissioned number must stop accepting patient traffic rather than
        keep writing into the clinic's records.
        """
        if not phone_number_id:
            return None
        result = await self._session.execute(
            text(
                "SELECT business_id FROM business_whatsapp_channels "
                "WHERE phone_number_id = :pnid AND status = 'active'"
            ),
            {"pnid": phone_number_id},
        )
        row = result.one_or_none()
        return None if row is None else int(row[0])

    async def resolve_phone_number_id(self, business_id: int) -> str | None:
        """Return the number a business should send from, or None.

        Preference order:
          1. the active primary channel, if one is designated;
          2. the sole active channel, if the business has exactly one.

        A business with several active channels and no designated primary is
        ambiguous and resolves to None rather than picking by row order. That
        state is unreachable through the registration path below, which always
        designates a primary, but it is reachable by direct SQL and must not
        silently send from an arbitrary number.
        """
        result = await self._session.execute(
            text(
                "SELECT phone_number_id, is_primary "
                "FROM business_whatsapp_channels "
                "WHERE business_id = :bid AND status = 'active' "
                "ORDER BY is_primary DESC, id ASC"
            ),
            {"bid": business_id},
        )
        rows = result.all()
        if not rows:
            return None
        if rows[0][1]:
            return str(rows[0][0])
        if len(rows) == 1:
            return str(rows[0][0])
        logger.error(
            "whatsapp_channel_ambiguous",
            extra={"business_id": business_id, "active_channels": len(rows)},
        )
        return None

    async def owns_channel(self, business_id: int, phone_number_id: str) -> bool:
        """Whether this tenant may send from this number.

        The send path already carries a phone_number_id chosen at enqueue time.
        Re-checking it at delivery closes the window where a channel was
        reassigned or disabled between enqueue and send.
        """
        owner = await self.resolve_business_id(phone_number_id)
        return owner is not None and owner == business_id


class ChannelOwnershipConflictError(Exception):
    """The provider number is already attached to a different tenant."""


async def register_channel(
    session: AsyncSession,
    *,
    business_id: int,
    phone_number_id: str,
    waba_id: str | None = None,
    display_phone_number: str | None = None,
    make_primary: bool = True,
) -> None:
    """Attach a provider number to a business. Caller owns the transaction.

    Raises ChannelOwnershipConflictError if the number already belongs to
    another tenant. Re-pointing a live number is a deliberate operator action
    with patient-visible consequences, so it must not happen as a side effect
    of onboarding, and it must not silently do nothing either.
    """
    existing = await session.execute(
        text(
            "SELECT business_id FROM business_whatsapp_channels "
            "WHERE phone_number_id = :pnid FOR UPDATE"
        ),
        {"pnid": phone_number_id},
    )
    owner_row = existing.one_or_none()
    if owner_row is not None and int(owner_row[0]) != business_id:
        raise ChannelOwnershipConflictError(
            f"phone_number_id is already registered to business_id={owner_row[0]}"
        )

    if make_primary:
        # Demote first: the partial unique index permits only one active
        # primary per business, so promoting without demoting would abort.
        await session.execute(
            text(
                "UPDATE business_whatsapp_channels SET is_primary = false, "
                "updated_at = NOW() "
                "WHERE business_id = :bid AND is_primary"
            ),
            {"bid": business_id},
        )

    await session.execute(
        text(
            "INSERT INTO business_whatsapp_channels "
            "(business_id, phone_number_id, waba_id, display_phone_number, "
            " status, is_primary, created_at, updated_at) "
            "VALUES (:bid, :pnid, :waba, :display, 'active', :primary, "
            " NOW(), NOW()) "
            "ON CONFLICT (phone_number_id) DO UPDATE SET "
            "  waba_id = EXCLUDED.waba_id, "
            "  display_phone_number = EXCLUDED.display_phone_number, "
            "  status = 'active', "
            "  is_primary = EXCLUDED.is_primary, "
            "  updated_at = NOW()"
        ),
        {
            "bid": business_id,
            "pnid": phone_number_id,
            "waba": waba_id,
            "display": display_phone_number,
            "primary": make_primary,
        },
    )
    await session.flush()
