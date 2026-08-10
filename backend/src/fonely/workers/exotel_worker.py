"""Inbound call event worker — claims and processes durable call events.

Feature-disabled until migration creates the inbound_call_events table
and calls.provider_call_id + calls.call_status columns.

GUARD: process_one() checks schema readiness before processing.
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.domain.calls.intake import ClaimedCallEvent
from fonely.domain.calls.transitions import LateCallEventError, is_terminal, validate_transition
from fonely.repositories.exotel_intake import InboundCallEventRepository

logger = logging.getLogger("fonely.workers.exotel_worker")


class StaleClaimError(Exception):
    """Fenced completion returned false — lease expired or claim stolen."""


class SchemaNotReadyError(Exception):
    """Worker cannot run without required schema."""


def _advisory_lock_key(business_id: int, provider_call_id: str) -> int:
    data = f"{business_id}:{provider_call_id}".encode()
    digest = hashlib.blake2b(data, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class InboundCallEventWorker:
    """Poll → guard → claim → advisory lock → domain mutation → complete/fail."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory
        self._schema_verified = False

    async def _verify_schema(self, session: AsyncSession) -> None:
        if self._schema_verified:
            return
        result = await session.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'calls' AND column_name = 'provider_call_id'"
            )
        )
        if result.scalar_one() == 0:
            raise SchemaNotReadyError(
                "calls.provider_call_id column missing — "
                "run migration before starting worker"
            )
        self._schema_verified = True

    async def process_one(self) -> bool:
        async with self._factory() as claim_session:
            await self._verify_schema(claim_session)
            repo = InboundCallEventRepository(claim_session)
            claimed = await repo.claim_next_eligible()
            if claimed is None:
                return False
            await claim_session.commit()

        try:
            async with self._factory() as session:
                lock_key = _advisory_lock_key(claimed.business_id, claimed.provider_call_id)
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
                            "provider_call_id": claimed.provider_call_id,
                            "status": claimed.status,
                        },
                    )
                repo = InboundCallEventRepository(session)
                ok = await repo.mark_completed(
                    claimed.id, claimed.business_id,
                    claimed.claim_token, claimed.claim_version,
                )
                if not ok:
                    await session.rollback()
                    raise StaleClaimError(
                        f"fenced completion false for event {claimed.id}"
                    )
                await session.commit()

            logger.info(
                "call_event_processed",
                extra={
                    "business_id": claimed.business_id,
                    "provider_call_id": claimed.provider_call_id,
                    "status": claimed.status,
                },
            )
            return True

        except StaleClaimError:
            logger.warning(
                "call_event_stale_claim",
                extra={"event_id": claimed.id, "provider_call_id": claimed.provider_call_id},
            )
            return False
        except Exception:
            logger.warning(
                "call_event_processing_failed",
                extra={
                    "business_id": claimed.business_id,
                    "provider_call_id": claimed.provider_call_id,
                },
                exc_info=True,
            )
            try:
                async with self._factory() as fail_session:
                    repo = InboundCallEventRepository(fail_session)
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
        """Apply call state using provider_call_id identity.

        Uses call_status column to preserve failed/busy/no_answer.
        No latest-call fallback. No ended_at-based status derivation.
        """
        existing = await session.execute(
            text(
                "SELECT id, call_status "
                "FROM calls "
                "WHERE business_id = :bid AND provider_call_id = :cid "
                "FOR UPDATE"
            ),
            {"bid": claimed.business_id, "cid": claimed.provider_call_id},
        )
        row = existing.one_or_none()

        if row is not None:
            current_status = row[1]
            validate_transition(current_status, claimed.status)
            if is_terminal(claimed.status):
                await session.execute(
                    text(
                        "UPDATE calls SET "
                        "  call_status = :status, "
                        "  ended_at = NOW(), "
                        "  duration_sec = :dur "
                        "WHERE id = :cid AND business_id = :bid"
                    ),
                    {
                        "status": claimed.status,
                        "cid": row[0],
                        "bid": claimed.business_id,
                        "dur": claimed.duration,
                    },
                )
            else:
                await session.execute(
                    text(
                        "UPDATE calls SET call_status = :status "
                        "WHERE id = :cid AND business_id = :bid"
                    ),
                    {"status": claimed.status, "cid": row[0], "bid": claimed.business_id},
                )
        else:
            await session.execute(
                text(
                    "INSERT INTO calls "
                    "(business_id, caller_phone, provider_call_id, call_status, "
                    " started_at, duration_sec, ended_at) "
                    "VALUES (:bid, :phone, :cid, :status, NOW(), :dur, "
                    "  CASE WHEN :terminal THEN NOW() ELSE NULL END)"
                ),
                {
                    "bid": claimed.business_id,
                    "phone": claimed.caller_phone,
                    "cid": claimed.provider_call_id,
                    "status": claimed.status,
                    "dur": claimed.duration,
                    "terminal": is_terminal(claimed.status),
                },
            )
        await session.flush()
