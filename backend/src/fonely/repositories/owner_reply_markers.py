"""Tenant-scoped persistence for the #43 owner-reply resume markers (P2 lane).

A marker is a ``pending_actions`` row with ``action_type='awaiting_owner_reply'``
(Dev4's 0021 contract). This repository provides ONLY the reads/writes the
inbound-worker owner branch needs to resolve an owner's WhatsApp reply against an
outstanding voice-call wait, plus the durable code-guess counter. It never
generates the correlation code (that is emitted voice-side at escalation, Dev4's
P1) — it only selects/counts and CAS-resolves.

CRITICAL PREDICATE SPLIT (frozen by delivery-readiness):
  * Dev4's DB UNIQUENESS spans BOTH active statuses (awaiting_owner_reply,
    resume_requested) to forbid a duplicate wait.
  * This lane's CARDINALITY/SELECTION counts and selects
    ``awaiting_owner_reply`` ONLY. A ``resume_requested`` marker is already
    answered and in the voice claim pipeline — it must never be counted,
    selected, or re-CAS'd. A coded reply hitting a resume_requested marker is a
    stale no-op (the service returns a reminder, not a second resolve).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.enums import PendingActionStatus, PendingActionType
from fonely.models.schema import OwnerReplyGuessAttempt, PendingAction

_AWAITING = PendingActionStatus.AWAITING_OWNER_REPLY.value


class OwnerReplyMarkerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_awaiting(self, business_id: int, *, now: datetime) -> int:
        """Number of ACTIVE-and-selectable markers for a tenant.

        Selectable == status AWAITING_OWNER_REPLY only (NOT resume_requested) and
        not past expiry. This is the cardinality gate's count; tenant isolation is
        the business_id predicate, always applied.
        """
        stmt = select(func.count()).select_from(PendingAction).where(
            PendingAction.business_id == business_id,
            PendingAction.action_type == PendingActionType.AWAITING_OWNER_REPLY.value,
            PendingAction.status == _AWAITING,
            PendingAction.expires_at > now,
        )
        return int((await self._session.scalar(stmt)) or 0)

    async def get_sole_awaiting(
        self, business_id: int, *, now: datetime
    ) -> PendingAction | None:
        """The unique selectable marker when the tenant has exactly one.

        Returns None if zero OR more than one (the caller must then require a
        correlation code). Locks the row FOR UPDATE so a concurrent resolve of the
        same marker serializes on it.
        """
        stmt = (
            select(PendingAction)
            .where(
                PendingAction.business_id == business_id,
                PendingAction.action_type == PendingActionType.AWAITING_OWNER_REPLY.value,
                PendingAction.status == _AWAITING,
                PendingAction.expires_at > now,
            )
            .with_for_update()
            .limit(2)
        )
        rows = list((await self._session.scalars(stmt)).all())
        return rows[0] if len(rows) == 1 else None

    async def get_by_code(
        self, business_id: int, correlation_code: str, *, now: datetime
    ) -> PendingAction | None:
        """The selectable marker whose code matches, tenant-scoped.

        Matches ONLY an AWAITING_OWNER_REPLY, unexpired marker — a code that maps
        to a resume_requested/expired/foreign marker resolves to None here (the
        service turns that into a stale/invalid-code reminder, never a resolve).
        The code is correlation-only; ``business_id`` (from the trusted actor) is
        what enforces tenant isolation, so business A's code can never select B's
        row. Locks the row FOR UPDATE.
        """
        stmt = (
            select(PendingAction)
            .where(
                PendingAction.business_id == business_id,
                PendingAction.action_type == PendingActionType.AWAITING_OWNER_REPLY.value,
                PendingAction.status == _AWAITING,
                PendingAction.correlation_code == correlation_code,
                PendingAction.expires_at > now,
            )
            .with_for_update()
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def list_awaiting_for_reminder(
        self, business_id: int, *, now: datetime, limit: int = 20
    ) -> list[PendingAction]:
        """The tenant's selectable markers, for composing the disambiguation
        reminder (codes + query_type only — the service redacts to non-PII)."""
        stmt = (
            select(PendingAction)
            .where(
                PendingAction.business_id == business_id,
                PendingAction.action_type == PendingActionType.AWAITING_OWNER_REPLY.value,
                PendingAction.status == _AWAITING,
                PendingAction.expires_at > now,
            )
            .order_by(PendingAction.created_at.asc(), PendingAction.id.asc())
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())

    # --- Durable code-guess rate limit (per business_id, owner_phone) ----------
    # A process-local counter would be a false claim across replicas (N replicas ->
    # N x the limit), so the count lives in PG. Increment + check run in the SAME
    # transaction as the resolve attempt.

    async def register_wrong_code_attempt(
        self,
        *,
        business_id: int,
        owner_phone: str,
        now: datetime,
        window: object,  # timedelta
        default_max_attempts: int = 5,
    ) -> tuple[int, int]:
        """Record one wrong-code guess and return (attempts_in_window, max).

        Rolling window keyed on (business_id, owner_phone): if the stored window is
        older than ``window`` the counter resets to 1 with a fresh window start;
        otherwise it increments. Upsert is atomic; the row's unique constraint
        (business_id, owner_phone) makes concurrent first-attempts collapse to one.
        """
        window_floor = now - window  # type: ignore[operator]
        insert_stmt = (
            pg_insert(OwnerReplyGuessAttempt)
            .values(
                business_id=business_id,
                owner_phone=owner_phone,
                attempts=1,
                max_attempts=default_max_attempts,
                window_started_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_owner_reply_guess_business_phone",
                set_={
                    # Reset when the existing window has aged out, else +1.
                    "attempts": case(
                        (
                            OwnerReplyGuessAttempt.window_started_at < window_floor,
                            1,
                        ),
                        else_=OwnerReplyGuessAttempt.attempts + 1,
                    ),
                    "window_started_at": case(
                        (
                            OwnerReplyGuessAttempt.window_started_at < window_floor,
                            now,
                        ),
                        else_=OwnerReplyGuessAttempt.window_started_at,
                    ),
                    "updated_at": now,
                },
            )
            .returning(OwnerReplyGuessAttempt.attempts, OwnerReplyGuessAttempt.max_attempts)
        )
        row = (await self._session.execute(insert_stmt)).one()
        return int(row.attempts), int(row.max_attempts)

    async def current_wrong_code_attempts(
        self, *, business_id: int, owner_phone: str, now: datetime, window: object
    ) -> int:
        """Attempts within the live window (0 if none or window aged out)."""
        window_floor = now - window  # type: ignore[operator]
        stmt = select(
            OwnerReplyGuessAttempt.attempts, OwnerReplyGuessAttempt.window_started_at
        ).where(
            OwnerReplyGuessAttempt.business_id == business_id,
            OwnerReplyGuessAttempt.owner_phone == owner_phone,
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None or row.window_started_at < window_floor:
            return 0
        return int(row.attempts)
