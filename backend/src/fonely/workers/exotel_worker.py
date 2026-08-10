"""Inbound call event worker — claims and processes durable call events.

Follows InboundWorker pattern. Feature-disabled until migration creates
the exotel_inbound_events table and calls.provider_call_sid column.

GUARD: process_one() checks schema readiness before processing.
The worker MUST NOT run without provider_call_sid identity — no
latest-call fallback.
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.domain.calls.intake import ClaimedCallEvent
from fonely.domain.calls.transitions import LateCallEventError, is_terminal, validate_transition
from fonely.repositories.exotel_intake import ExotelInboundEventRepository

logger = logging.getLogger("fonely.workers.exotel_worker")


class StaleClaimError(Exception):
    """Fenced completion returned false — lease expired or another worker took the claim."""


class SchemaNotReadyError(Exception):
    """Worker cannot run without required schema (provider_call_sid column)."""


def _advisory_lock_key(business_id: int, call_sid: str) -> int:
    """Deterministic advisory lock key for (business_id, call_sid)."""
    data = f"{business_id}:{call_sid}".encode()
    digest = hashlib.blake2b(data, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class InboundCallEventWorker:
    """Poll → guard → claim → advisory lock → validate → domain mutation → complete/fail.

    Schema guard: refuses to process if calls.provider_call_sid column
    does not exist. This prevents the unsafe latest-call fallback.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory
        self._schema_verified = False

    async def _verify_schema(self, session: AsyncSession) -> None:
        """Check that required schema exists. Raises SchemaNotReadyError if not.

        Scopes to current_schema() to avoid false positives from other
        schemas in the same database. Uses COUNT to handle deterministically
        even if multiple rows match (should not happen, but defensive).
        """
        if self._schema_verified:
            return
        result = await session.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'calls' AND column_name = 'provider_call_sid'"
            )
        )
        if result.scalar_one() == 0:
            raise SchemaNotReadyError(
                "calls.provider_call_sid column missing — "
                "run migration before starting worker"
            )
        self._schema_verified = True

    async def process_one(self) -> bool:
        """Process one eligible event. Returns True if processed."""
        async with self._factory() as claim_session:
            await self._verify_schema(claim_session)
            repo = ExotelInboundEventRepository(claim_session)
            claimed = await repo.claim_next_eligible()
            if claimed is None:
                return False
            await claim_session.commit()

        try:
            async with self._factory() as session:
                lock_key = _advisory_lock_key(claimed.business_id, claimed.call_sid)
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": lock_key},
                )

                try:
                    await self._apply_domain_mutation(session, claimed)
                except LateCallEventError:
                    logger.info(
                        "worker_ooo_noop",
                        extra={
                            "business_id": claimed.business_id,
                            "call_sid": claimed.call_sid,
                            "status": claimed.status,
                        },
                    )
                repo = ExotelInboundEventRepository(session)
                ok = await repo.mark_completed(
                    claimed.id, claimed.business_id,
                    claimed.claim_token, claimed.claim_version,
                )
                if not ok:
                    await session.rollback()
                    raise StaleClaimError(
                        f"fenced completion false for event {claimed.id} "
                        f"(lease expired or claim stolen)"
                    )
                await session.commit()

            logger.info(
                "call_event_processed",
                extra={
                    "business_id": claimed.business_id,
                    "call_sid": claimed.call_sid,
                    "status": claimed.status,
                },
            )
            return True

        except StaleClaimError:
            logger.warning(
                "call_event_stale_claim",
                extra={"event_id": claimed.id, "call_sid": claimed.call_sid},
            )
            return False
        except Exception:
            logger.warning(
                "call_event_processing_failed",
                extra={"business_id": claimed.business_id, "call_sid": claimed.call_sid},
                exc_info=True,
            )
            try:
                async with self._factory() as fail_session:
                    repo = ExotelInboundEventRepository(fail_session)
                    await repo.mark_failed(
                        claimed.id, claimed.business_id,
                        claimed.claim_token, claimed.claim_version,
                    )
                    await fail_session.commit()
            except Exception:
                logger.error("call_event_failure_recording_failed", exc_info=True)
            return False

    async def _apply_domain_mutation(
        self, session: AsyncSession, claimed: ClaimedCallEvent
    ) -> None:
        """Apply call state using CallSid-based identity.

        Looks up by (business_id, provider_call_sid). No latest-call fallback.
        Creates a new call record if no matching CallSid exists.
        Raises LateCallEventError if a terminal already processed.
        """
        existing = await session.execute(
            text(
                "SELECT id, "
                "  CASE WHEN ended_at IS NOT NULL THEN 'completed' "
                "       ELSE 'in-progress' END as current_status "
                "FROM calls "
                "WHERE business_id = :bid AND provider_call_sid = :sid "
                "FOR UPDATE"
            ),
            {"bid": claimed.business_id, "sid": claimed.call_sid},
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
                    "INSERT INTO calls (business_id, caller_phone, provider_call_sid, "
                    "  started_at, duration_sec, ended_at) "
                    "VALUES (:bid, :phone, :sid, NOW(), :dur, "
                    "  CASE WHEN :terminal THEN NOW() ELSE NULL END)"
                ),
                {
                    "bid": claimed.business_id,
                    "phone": claimed.caller_phone,
                    "sid": claimed.call_sid,
                    "dur": claimed.duration,
                    "terminal": is_terminal(claimed.status),
                },
            )
        await session.flush()
