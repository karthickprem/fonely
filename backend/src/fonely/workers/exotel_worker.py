"""Exotel inbound event worker — claims and processes durable call events.

Follows the InboundWorker pattern. Feature-disabled until migration.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.domain.calls.intake import ClaimedCallEvent
from fonely.domain.calls.transitions import is_terminal, validate_transition
from fonely.repositories.exotel_intake import ExotelInboundEventRepository

logger = logging.getLogger("fonely.workers.exotel_worker")


class StaleClaimError(Exception):
    """Fenced completion returned false — another worker took the claim."""


class ExotelInboundWorker:
    """Poll → claim → validate transition → domain mutation → complete/fail."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def process_one(self) -> bool:
        """Process one eligible event. Returns True if processed."""
        async with self._factory() as session:
            repo = ExotelInboundEventRepository(session)
            claimed = await repo.claim_next_eligible()
            if claimed is None:
                return False

        try:
            async with self._factory() as session:
                await self._apply_domain_mutation(session, claimed)
                repo = ExotelInboundEventRepository(session)
                ok = await repo.mark_completed(
                    claimed.id, claimed.claim_token, claimed.claim_version
                )
                if not ok:
                    await session.rollback()
                    raise StaleClaimError(f"fenced completion false for event {claimed.id}")
                await session.commit()

            logger.info(
                "exotel_event_processed",
                extra={
                    "business_id": claimed.business_id,
                    "call_sid": claimed.call_sid,
                    "status": claimed.status,
                },
            )
            return True

        except StaleClaimError:
            logger.warning(
                "exotel_event_stale_claim",
                extra={"event_id": claimed.id, "call_sid": claimed.call_sid},
            )
            return False
        except Exception:
            logger.warning(
                "exotel_event_processing_failed",
                extra={"business_id": claimed.business_id, "call_sid": claimed.call_sid},
                exc_info=True,
            )
            try:
                async with self._factory() as session:
                    repo = ExotelInboundEventRepository(session)
                    await repo.mark_failed(claimed.id, claimed.claim_token, claimed.claim_version)
            except Exception:
                logger.error("exotel_event_failure_recording_failed", exc_info=True)
            return False

    async def _apply_domain_mutation(
        self, session: AsyncSession, claimed: ClaimedCallEvent
    ) -> None:
        """Apply call state using CallSid identity with forward-only transitions."""
        existing = await session.execute(
            text(
                "SELECT id, "
                "  CASE WHEN ended_at IS NOT NULL THEN 'completed' "
                "       ELSE 'in-progress' END as current_status "
                "FROM calls "
                "WHERE business_id = :bid "
                "ORDER BY started_at DESC LIMIT 1"
            ),
            {"bid": claimed.business_id},
        )
        row = existing.one_or_none()

        if row is not None:
            current_status = row[1]
            validate_transition(current_status, claimed.status)
            if is_terminal(claimed.status):
                await session.execute(
                    text(
                        "UPDATE calls SET ended_at = NOW(), duration_sec = :dur "
                        "WHERE id = :cid AND business_id = :bid"
                    ),
                    {"cid": row[0], "bid": claimed.business_id, "dur": claimed.duration},
                )
        else:
            await session.execute(
                text(
                    "INSERT INTO calls (business_id, caller_phone, started_at, "
                    "  duration_sec, ended_at) "
                    "VALUES (:bid, :phone, NOW(), :dur, "
                    "  CASE WHEN :terminal THEN NOW() ELSE NULL END)"
                ),
                {
                    "bid": claimed.business_id,
                    "phone": claimed.caller_phone,
                    "dur": claimed.duration,
                    "terminal": is_terminal(claimed.status),
                },
            )
        await session.flush()
