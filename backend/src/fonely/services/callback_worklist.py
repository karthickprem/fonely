"""Owner-facing worklist over durable voice-give-up callbacks (#41, part B).

#36 persists a CALLBACK pending action when a voice caller could not finish
booking (doctor disambiguation exhausted). Those rows were durable but INVISIBLE
— nothing let the clinic owner see or act on them. This is the owner-facing
QUERY + RESOLVE surface over them:

  * list_pending — the callbacks a business still owes a call-back on, tenant
    scoped (an owner sees ONLY their own business's callbacks), newest first,
    with the partial booking facts a human needs to resume.
  * resolve — the owner marks one handled. It transitions to a terminal status
    so it stops surfacing AND becomes retention-eligible on the normal schedule.

PARTIAL CLOSE — this makes callbacks QUERYABLE and RESOLVABLE, but does NOT yet
actively PUSH them to the owner (no WhatsApp/notification). The #41 gap ("nobody
calls the patient back") closes only when the owner is TOLD, which is part A
(a CALLBACK_REQUESTED notification event, gated on migration 0020 + founder
auth). Until A ships, an owner must PULL this worklist. Do not read this module's
existence as "#41 closed".

No new schema: pending_actions already carries status/confirmed_by/confirmed_at.
Resolve reuses them; there is no resolved_at column and none is needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.validators import utcnow
from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.errors import (
    PendingActionConcurrencyError,
    PendingActionNotFoundError,
)
from fonely.domain.pending_actions.transitions import assert_transition_allowed
from fonely.models.enums import PendingActionStatus, PendingActionType
from fonely.models.schema import PendingAction
from fonely.repositories.pending_actions import PendingActionRepository
from fonely.services.authorization import require_owner_or_manager

# A callback is created with status COLLECTING_DETAILS (PendingActionService
# .create). While it is in a non-terminal, still-expirable status it is an
# open item the owner still owes a call-back on.
_PENDING_CALLBACK_STATUSES: tuple[str, ...] = (
    PendingActionStatus.COLLECTING_DETAILS.value,
    PendingActionStatus.AWAITING_CONFIRMATION.value,
)

# Resolving a callback means "the owner handled this" — it never becomes an
# appointment (a callback commits no entity), so CONFIRMED (which means a booking
# committed, and is not even reachable from COLLECTING_DETAILS) is wrong. CANCELLED
# is the terminal status reachable from both pending statuses; it stops surfacing
# and is retention-eligible.
_RESOLVED_STATUS = PendingActionStatus.CANCELLED
_RESOLVED_REASON_CODE = "owner_handled"


@dataclass(frozen=True)
class CallbackItem:
    """One pending callback, flattened for an owner surface."""

    pending_action_id: int
    version: int
    status: str
    caller_phone: str
    reason_code: str
    service_id: int | None
    service_name: str | None
    target_date: str | None
    attempted_candidates: tuple[str, ...]
    requested_at: str | None


class CallbackWorklistService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = PendingActionRepository(session)

    async def list_pending(self, actor: ActorContext, *, limit: int = 100) -> list[CallbackItem]:
        """Pending callbacks for the actor's business. Owner/manager only, and
        tenant-scoped to actor.business_id — the trusted business scope, never a
        caller-supplied value."""
        await require_owner_or_manager(self._session, actor)
        rows = await self._repo.list_pending_by_type(
            actor.business_id,
            PendingActionType.CALLBACK,
            _PENDING_CALLBACK_STATUSES,
            limit=limit,
        )
        return [self._to_item(row) for row in rows]

    async def resolve(
        self, actor: ActorContext, pending_action_id: int, expected_version: int
    ) -> CallbackItem:
        """Owner marks a callback handled -> terminal CANCELLED. Owner/manager
        only; tenant-scoped; optimistic-concurrency guarded on version."""
        await require_owner_or_manager(self._session, actor)

        action = await self._repo.get_by_id(actor.business_id, pending_action_id)
        if action is None or action.action_type != PendingActionType.CALLBACK.value:
            # A non-callback id, or another tenant's id (get_by_id is already
            # business-scoped, so a foreign id simply is not found), reads the
            # same to the owner: there is no such callback in your worklist.
            raise PendingActionNotFoundError("No such callback for this business")

        current = PendingActionStatus(action.status)
        # Fail closed on an already-resolved (terminal) callback rather than
        # silently no-op: a double-resolve is a real signal (two owners racing,
        # or a stale worklist), and assert_transition_allowed surfaces it.
        assert_transition_allowed(current, _RESOLVED_STATUS)

        updated = await self._repo.conditional_update(
            business_id=actor.business_id,
            action_id=pending_action_id,
            expected_version=expected_version,
            expected_status=current,
            values={
                "status": _RESOLVED_STATUS.value,
                "rejection_reason_code": _RESOLVED_REASON_CODE,
                "confirmed_by": actor.normalized_phone,
                "confirmed_at": utcnow(),
            },
        )
        if updated is None:
            # version/status moved under us: someone resolved or advanced it first.
            raise PendingActionConcurrencyError(
                "Callback was modified concurrently; refresh the worklist"
            )
        return self._to_item(updated)

    @staticmethod
    def _to_item(action: PendingAction) -> CallbackItem:
        payload = action.proposed_payload or {}
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}
        raw_candidates = data.get("attempted_candidates", [])
        candidates = (
            tuple(str(c) for c in raw_candidates if isinstance(c, str))
            if isinstance(raw_candidates, list)
            else ()
        )
        return CallbackItem(
            pending_action_id=action.id,
            version=action.version,
            status=action.status,
            caller_phone=str(data.get("caller_phone", "")),
            reason_code=str(data.get("reason_code", "")),
            service_id=data.get("service_id") if isinstance(data.get("service_id"), int) else None,
            service_name=(
                str(data["service_name"]) if isinstance(data.get("service_name"), str) else None
            ),
            target_date=(
                str(data["target_date"]) if isinstance(data.get("target_date"), str) else None
            ),
            attempted_candidates=candidates,
            requested_at=(
                str(data["requested_at"]) if isinstance(data.get("requested_at"), str) else None
            ),
        )
