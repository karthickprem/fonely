"""#43 P3: the voice-process claim loop that delivers an owner reply into a
suspended live call.

The inbound WhatsApp worker runs in a SEPARATE process; when the owner replies it
CAS-flips the durable marker to ``RESUME_REQUESTED`` and persists the answer, but
it cannot reach this process's in-memory ResumeRegistry. Postgres is the only
channel. This background loop closes it: each tick it claims RESUME_REQUESTED
markers whose call is one THIS replica is actually holding, reconciles the owner's
(untrusted) free-text answer through the availability engine into real bookable
slots, and speaks those slots into the live call via the registry.

Replica ownership is the claim filter: a marker is claimed ONLY by the replica
whose local registry holds its ``(business_id, call_id)`` — the process serving
that WebRTC call. A replica that does not hold the call leaves the marker
untouched (never claims, never resolves stale). If the owning replica died, no
replica holds the call, nobody claims, and the marker ages to EXPIRED via the
authoritative sweep (never via a non-owner's local absence).

Exactly-once across processes is the status CAS itself: claiming a marker IS
``conditional_update(RESUME_REQUESTED -> RESUMED)`` on the shared status + version.
The single UPDATE that flips the row wins; a racing claimer or the expiry sweep
gets rowcount 0 and does nothing. No separate lease/token columns are needed — the
status+version CAS is the fence.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from fonely.domain.pending_actions.payloads import AwaitingOwnerReplyData
from fonely.models.enums import PendingActionStatus, PendingActionType
from fonely.models.schema import PendingAction

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession

    from .resume_registry import ResumeRegistry

logger = logging.getLogger("fonely.voice.owner_reply_claim_loop")

_DEFAULT_POLL_INTERVAL = 1.0
_MAX_BACKOFF = 30.0


async def run_owner_reply_claim_loop(
    *,
    resume_registry: ResumeRegistry,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    reconcile_answer: Callable[[AsyncSession, int, AwaitingOwnerReplyData], Awaitable[str]],
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    max_iterations: int | None = None,
) -> None:
    """Poll for RESUME_REQUESTED markers on locally-held calls and resume them.

    Runs until ``max_iterations`` ticks elapse (None = forever, for the lifespan
    background task; a bounded count for tests). A transient failure on one tick
    backs off exponentially rather than killing the loop — the table stays truth,
    so a missed tick is retried, never a lost resume.

    ``reconcile_answer(session, business_id, data)`` turns the marker's persisted
    (untrusted) owner answer into the caller-facing text — real availability, not
    the owner's words verbatim. Injected so the loop stays free of engine wiring
    and tests can supply a deterministic reconciler.
    """
    iterations = 0
    consecutive_failures = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            await _claim_tick(
                resume_registry=resume_registry,
                session_factory=session_factory,
                reconcile_answer=reconcile_answer,
            )
            consecutive_failures = 0
            await asyncio.sleep(poll_interval)
        except Exception:
            consecutive_failures += 1
            logger.error(
                "owner_reply_claim_tick_failed",
                extra={"consecutive_failures": consecutive_failures},
            )
            await asyncio.sleep(min(_MAX_BACKOFF, poll_interval * 2**consecutive_failures))


async def _claim_tick(
    *,
    resume_registry: ResumeRegistry,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    reconcile_answer: Callable[[AsyncSession, int, AwaitingOwnerReplyData], Awaitable[str]],
) -> int:
    """One poll: claim + resume every RESUME_REQUESTED marker for a locally-held
    call. Returns the number of calls resumed this tick (0 when idle)."""
    local_keys = await resume_registry.local_keys()
    if not local_keys:
        return 0

    resumed = 0
    for business_id, call_id in local_keys:
        if await _claim_and_resume(
            resume_registry=resume_registry,
            session_factory=session_factory,
            reconcile_answer=reconcile_answer,
            business_id=business_id,
            call_id=call_id,
        ):
            resumed += 1
    return resumed


async def _claim_and_resume(
    *,
    resume_registry: ResumeRegistry,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    reconcile_answer: Callable[[AsyncSession, int, AwaitingOwnerReplyData], Awaitable[str]],
    business_id: int,
    call_id: int,
) -> bool:
    """Claim the RESUME_REQUESTED marker for one held call and resume it.

    The claim is the status CAS (RESUME_REQUESTED -> RESUMED): exactly one claimer
    wins across replicas/processes, the rest get rowcount 0. Only the winner reads
    the answer, reconciles it, and speaks it — so the reconciled slots reach the
    call exactly once. The read goes through the digest-VALIDATING path, so a
    payload whose digest drifted (a bug) fails loud rather than speaking stale
    facts.
    """
    from fonely.repositories.pending_actions import PendingActionRepository

    async with session_factory() as session:
        marker = await _load_resume_requested_marker(session, business_id, call_id)
        if marker is None:
            return False

        # Validate the stored payload against its digest BEFORE acting on the
        # answer — a mismatch means corruption/a mutation that skipped the digest
        # recompute, and we must not speak facts derived from an unverified
        # payload. Fails loud (the loop's error handler logs + backs off).
        data = _validated_answer_data(marker)

        # Reconcile the owner's UNTRUSTED answer into real bookable slots. Done
        # BEFORE the claim CAS so a reconciliation failure doesn't burn the marker
        # (it stays RESUME_REQUESTED and is retried next tick).
        spoken = await reconcile_answer(session, business_id, data)

        # THE CLAIM: flip RESUME_REQUESTED -> RESUMED. Exactly one winner; a racing
        # claimer or the sweep gets None here and this replica does nothing.
        repo = PendingActionRepository(session)
        claimed = await repo.conditional_update(
            business_id=business_id,
            action_id=marker.id,
            expected_version=marker.version,
            expected_status=PendingActionStatus.RESUME_REQUESTED,
            values={"status": PendingActionStatus.RESUMED.value},
        )
        if claimed is None:
            # Lost the race (another claimer, or the sweep expired it) — no speech.
            await session.rollback()
            return False
        await session.commit()

    # Marker is durably RESUMED; now speak into the live call. The registry's own
    # resumed_once latch is the in-process terminal-turn guard (resume vs the
    # guard-c timeout fallback — exactly one speaks into this call).
    spoke = await resume_registry.resume(business_id, call_id, spoken)
    logger.info(
        "owner_reply_claim_resumed",
        extra={"business_id": business_id, "call_id": call_id, "spoke": spoke},
    )
    return spoke


async def _load_resume_requested_marker(
    session: AsyncSession, business_id: int, call_id: int
) -> PendingAction | None:
    """The single active RESUME_REQUESTED marker for this call, or None.

    Selects RESUME_REQUESTED ONLY (not AWAITING_OWNER_REPLY — that's still waiting
    on the owner) for the trusted (business_id, call_id). FOR UPDATE SKIP LOCKED so
    concurrent claimers don't block each other; the status CAS is the real fence.
    """
    stmt = (
        select(PendingAction)
        .where(
            PendingAction.business_id == business_id,
            PendingAction.call_id == call_id,
            PendingAction.action_type == PendingActionType.AWAITING_OWNER_REPLY.value,
            PendingAction.status == PendingActionStatus.RESUME_REQUESTED.value,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _validated_answer_data(marker: PendingAction) -> AwaitingOwnerReplyData:
    """Read the marker's answer payload through the digest-VALIDATING path.

    Reuses the shared pending-action validation so a payload/digest mismatch
    raises here (the exact cross-lane seam: if P2 mutated the payload without
    recomputing the digest, this loud failure is far better than silently speaking
    a stale answer). Returns the typed answer data."""
    from fonely.domain.pending_actions.payloads import PendingAwaitingOwnerReplyEnvelope
    from fonely.domain.pending_actions.snapshots import payload_digest

    envelope = PendingAwaitingOwnerReplyEnvelope.model_validate(marker.proposed_payload)
    if payload_digest(envelope) != marker.payload_digest:
        msg = "owner-reply marker payload digest mismatch — refusing to resume on unverified answer"
        raise ValueError(msg)
    return envelope.data
