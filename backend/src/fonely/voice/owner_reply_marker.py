"""#43 P1: write the durable owner-reply-resume marker at escalation.

When the voice agent escalates to the owner mid-call and suspends the line, it
writes a ``pending_actions`` row (``action_type='awaiting_owner_reply'``,
``status=AWAITING_OWNER_REPLY``) carrying the TRUSTED call identity on real
columns (business_id, call_id, query_type) plus a short human-safe correlation
code. That row is the durable, cross-process correlation handle: the inbound
worker (a SEPARATE process) flips it to ``resume_requested`` and persists the
owner's answer when the reply arrives, and the voice claim loop picks it up.

The marker WRITE is caller-transactional — it commits on the escalation's own DB
session, so "marker exists" implies "the escalation durably happened" even across
a process restart (unlike the old in-memory, fire-and-forget ask_doctor).
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from fonely.domain.pending_actions.payloads import (
    OWNER_REPLY_CODE_ALPHABET,
    OWNER_REPLY_CODE_LENGTH,
    build_awaiting_owner_reply_payload,
)
from fonely.domain.pending_actions.snapshots import awaiting_owner_reply_payload_digest
from fonely.models.enums import PendingActionStatus, PendingActionType
from fonely.models.schema import PendingAction

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("fonely.voice.owner_reply_marker")

# The unique constraint the code collides against — collision-retry regenerates
# ONLY on this exact violation, so an unrelated IntegrityError is never masked.
_CODE_UNIQUE_CONSTRAINT = "uq_pending_actions_business_correlation_code"
_MAX_CODE_ATTEMPTS = 8


def generate_correlation_code() -> str:
    """A cryptographically-random, human-safe correlation code.

    ``secrets`` (not ``random``) so the code is unpredictable — a guessable code
    (a counter, or a hash of call_id) would leak the call identity and be
    brute-forceable. Drawn from the shared alphabet/length so the P2 parser
    matches it exactly.
    """
    return "".join(
        secrets.choice(OWNER_REPLY_CODE_ALPHABET) for _ in range(OWNER_REPLY_CODE_LENGTH)
    )


def _code_suffix(code: str) -> str:
    """A log-safe fragment — never the whole code beside PII."""
    return f"...{code[-2:]}" if len(code) >= 2 else "..."


async def write_awaiting_owner_reply_marker(
    session: AsyncSession,
    *,
    business_id: int,
    call_id: int,
    query_type: str,
    idempotency_key: str,
    expires_at: datetime,
    now: datetime,
) -> str | None:
    """Insert the marker and return its correlation code, or None if a marker for
    this call+query is already active (the one-active-wait unique index caught a
    duplicate escalation — do not stack a second wait).

    All identity comes from the TRUSTED caller (business_id, call_id from the
    admitted session), never model output. The code is generated here and
    regenerated on a code-collision (bounded); a collision on the one-active-wait
    index instead means an active marker already exists → return None.

    Does NOT commit — the caller owns the transaction so the marker is written
    with the escalation's other effects (caller-transactional).
    """
    # The initial payload carries no answer yet. Its digest is set here and MUST
    # be recomputed whenever the payload mutates (P2's answer-persist recomputes
    # it too), so any digest-validating read path stays consistent — otherwise a
    # consumer that validates the stored payload would raise on every answered
    # marker and the resume would silently never fire.
    initial_payload = build_awaiting_owner_reply_payload()
    initial_digest = awaiting_owner_reply_payload_digest(initial_payload)

    for _ in range(_MAX_CODE_ATTEMPTS):
        code = generate_correlation_code()
        marker = PendingAction(
            business_id=business_id,
            action_type=PendingActionType.AWAITING_OWNER_REPLY.value,
            status=PendingActionStatus.AWAITING_OWNER_REPLY.value,
            proposed_payload=initial_payload,
            payload_digest=initial_digest,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            call_id=call_id,
            query_type=query_type,
            correlation_code=code,
            version=1,
        )
        session.add(marker)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            constraint = _violated_constraint(exc)
            if constraint == _CODE_UNIQUE_CONSTRAINT:
                # Code clash among the business's active waits — regenerate.
                continue
            if constraint == "uq_pending_actions_one_active_wait":
                # A wait for this call+query already exists — don't duplicate.
                logger.info(
                    "owner_reply_marker_already_active",
                    extra={"business_id": business_id, "call_id": call_id},
                )
                return None
            if constraint == "uq_pending_idempotency":
                # Same escalation replayed (same idempotency key) — already recorded.
                return None
            raise
        else:
            logger.info(
                "owner_reply_marker_written",
                extra={
                    "business_id": business_id,
                    "call_id": call_id,
                    "code_suffix": _code_suffix(code),
                },
            )
            return code

    # Exhausted code attempts — astronomically unlikely with 31**4 codes and the
    # tiny active-wait set. Fail loud rather than silently not escalate.
    msg = "could not generate a unique owner-reply correlation code after retries"
    raise RuntimeError(msg)


def _violated_constraint(exc: IntegrityError) -> str | None:
    """The constraint name from an asyncpg UniqueViolation, or None."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate != "23505":  # unique_violation
        return None
    constraint = getattr(orig, "constraint_name", None)
    if constraint:
        return str(constraint)
    text = str(orig)
    for name in (
        _CODE_UNIQUE_CONSTRAINT,
        "uq_pending_actions_one_active_wait",
        "uq_pending_idempotency",
    ):
        if name in text:
            return name
    return None
