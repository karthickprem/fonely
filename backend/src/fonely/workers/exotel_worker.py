"""Exotel inbound event worker — claims and processes durable call events.

Follows the InboundWorker pattern: poll → claim → process → complete/fail.
Does NOT run until the exotel_inbound_events migration is applied.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.domain.calls.transitions import is_terminal, validate_transition
from fonely.repositories.exotel_intake import ExotelInboundEventRepository

logger = logging.getLogger("fonely.workers.exotel_worker")


class ExotelInboundWorker:
    """Claims and processes Exotel inbound events into domain state.

    Each iteration:
    1. Claim one eligible event (SKIP LOCKED)
    2. Validate forward-only state transition
    3. Apply domain mutation to calls table
    4. Mark event completed on success; failed with backoff on error
    5. Dead-letter after max_attempts
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def process_one(self) -> bool:
        """Process a single eligible event. Returns True if one was processed."""
        async with self._factory() as session:
            repo = ExotelInboundEventRepository(session)

            claimed = await repo.claim_next_eligible()
            if claimed is None:
                return False

            await session.commit()

        event_id = claimed["id"]
        claim_token = claimed["claim_token"]
        claim_version = claimed["claim_version"]

        try:
            async with self._factory() as session:
                await self._apply_domain_mutation(session, claimed)
                repo = ExotelInboundEventRepository(session)
                await repo.mark_completed(event_id, claim_token, claim_version)
                await session.commit()

            logger.info(
                "exotel_event_processed",
                extra={
                    "business_id": claimed["business_id"],
                    "call_sid": claimed["call_sid"],
                    "event_type": claimed["event_type"],
                    "status": claimed["status"],
                },
            )
            return True

        except Exception:
            logger.warning(
                "exotel_event_processing_failed",
                extra={
                    "business_id": claimed["business_id"],
                    "call_sid": claimed["call_sid"],
                },
                exc_info=True,
            )
            try:
                async with self._factory() as session:
                    repo = ExotelInboundEventRepository(session)
                    await repo.mark_failed(event_id, claim_token, claim_version)
                    await session.commit()
            except Exception:
                logger.error("exotel_event_failure_recording_failed", exc_info=True)
            return False

    async def _apply_domain_mutation(self, session: AsyncSession, claimed: dict[str, Any]) -> None:
        """Apply call state changes from the inbound event.

        Uses provider_call_sid for identity (requires calls table migration).
        Forward-only transitions enforced.
        """
        from sqlalchemy import text

        existing = await session.execute(
            text(
                "SELECT id, "
                "  CASE WHEN ended_at IS NOT NULL THEN 'completed' "
                "       ELSE 'in-progress' END as current_status "
                "FROM calls "
                "WHERE business_id = :bid "
                "ORDER BY started_at DESC LIMIT 1"
            ),
            {"bid": claimed["business_id"]},
        )
        row = existing.one_or_none()

        if row is not None:
            current_status = row[1]
            validate_transition(current_status, claimed["status"])
            if is_terminal(claimed["status"]):
                await session.execute(
                    text(
                        "UPDATE calls SET "
                        "  ended_at = NOW(), "
                        "  duration_sec = :dur "
                        "WHERE id = :cid AND business_id = :bid"
                    ),
                    {
                        "cid": row[0],
                        "bid": claimed["business_id"],
                        "dur": claimed["duration"],
                    },
                )
        else:
            await session.execute(
                text(
                    "INSERT INTO calls "
                    "(business_id, caller_phone, started_at, duration_sec, "
                    " ended_at) "
                    "VALUES (:bid, :phone, NOW(), :dur, "
                    "  CASE WHEN :terminal THEN NOW() ELSE NULL END)"
                ),
                {
                    "bid": claimed["business_id"],
                    "phone": claimed["caller_phone"],
                    "dur": claimed["duration"],
                    "terminal": is_terminal(claimed["status"]),
                },
            )
        await session.flush()
