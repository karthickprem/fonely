"""#43 P3 reconciliation: turn an owner's UNTRUSTED free-text availability reply
into REAL bookable slots the agent can speak.

The owner is authenticated, but "free 6-10pm" is INPUT, not authoritative
availability — the same trust discipline as everywhere. The owner's reply means
"I've now confirmed this day's schedule"; the agent must speak the slots the
BOOKING ENGINE actually derives (operating hours minus exceptions minus existing
bookings), never the owner's words verbatim. So a broad "6-10pm" cannot become
spoken slots that bypass real availability, and the caller is offered only times
that are genuinely bookable.

This routes through the SAME engine the booking path uses
(``clinic_resolver.available_slots_text`` → ``AvailabilityService``), not a
parallel re-implementation — a second availability code path would be its own
drift/correctness risk.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from fonely.domain.pending_actions.payloads import AwaitingOwnerReplyData

logger = logging.getLogger("fonely.voice.owner_reply_reconcile")


async def reconcile_owner_answer(
    session: AsyncSession,
    business_id: int,
    data: AwaitingOwnerReplyData,
    *,
    target_date: date,
    timezone: str,
) -> str:
    """Reconcile the owner's persisted answer into caller-facing availability.

    The owner's raw ``data.answer_text`` is NOT spoken. Instead we re-query the
    engine for ``target_date`` — the owner's reply is the signal that the day's
    schedule is now confirmed, and the engine returns the real bookable slots
    (empty → a graceful "still couldn't confirm" line). The result is what the
    agent speaks; the untrusted text never reaches the caller.
    """
    from . import clinic_resolver

    slots_text = await clinic_resolver.available_slots_text(
        session, business_id, timezone, target_date
    )
    has_slots = "No confirmed availability" not in slots_text
    logger.info(
        "owner_reply_reconciled",
        extra={"business_id": business_id, "has_slots": has_slots},
    )
    return slots_text
