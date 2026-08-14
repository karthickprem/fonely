"""Automated data retention cleanup for Fonely."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.retention import get_retention_policies

logger = logging.getLogger("fonely.services.data_retention")

_BATCH_SIZE = 1000
_TERMINAL_CONVERSATION_STATES = ("completed", "ended", "escalated")
_TERMINAL_PA_STATUSES = ("confirmed", "rejected", "expired")


@dataclass
class RetentionResult:
    conversations_deleted: int = 0
    turns_deleted: int = 0
    notifications_deleted: int = 0
    pending_actions_deleted: int = 0
    callbacks_deleted: int = 0
    inbound_events_deleted: int = 0
    call_transcripts_redacted: int = 0
    execution_time_ms: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "conversations_deleted": self.conversations_deleted,
            "turns_deleted": self.turns_deleted,
            "notifications_deleted": self.notifications_deleted,
            "pending_actions_deleted": self.pending_actions_deleted,
            "callbacks_deleted": self.callbacks_deleted,
            "inbound_events_deleted": self.inbound_events_deleted,
            "call_transcripts_redacted": self.call_transcripts_redacted,
            "execution_time_ms": round(self.execution_time_ms, 1),
        }


class DataRetentionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run_cleanup(self) -> RetentionResult:
        start = time.monotonic()
        policies = get_retention_policies()
        now = datetime.now(UTC)
        result = RetentionResult()

        conv_cutoff = now - timedelta(days=policies["conversations"].retention_days)
        conv_count, turn_count = await self._cleanup_conversations(conv_cutoff)
        result.conversations_deleted = conv_count
        result.turns_deleted = turn_count

        delivered_cutoff = now - timedelta(days=policies["notifications_delivered"].retention_days)
        dead_cutoff = now - timedelta(days=policies["notifications_dead_letter"].retention_days)
        result.notifications_deleted = await self._cleanup_notifications(
            delivered_cutoff, dead_cutoff
        )

        pa_cutoff = now - timedelta(days=policies["pending_actions"].retention_days)
        result.pending_actions_deleted = await self._cleanup_pending_actions(pa_cutoff)

        callback_cutoff = now - timedelta(days=policies["callbacks"].retention_days)
        result.callbacks_deleted = await self._cleanup_callbacks(callback_cutoff)

        inbound_completed_cutoff = now - timedelta(
            days=policies["whatsapp_inbound_completed"].retention_days
        )
        inbound_dead_cutoff = now - timedelta(
            days=policies["whatsapp_inbound_dead_letter"].retention_days
        )
        result.inbound_events_deleted = await self._cleanup_inbound_events(
            inbound_completed_cutoff, inbound_dead_cutoff
        )

        transcript_cutoff = now - timedelta(days=policies["call_transcripts"].retention_days)
        result.call_transcripts_redacted = await self._redact_call_transcripts(transcript_cutoff)

        result.execution_time_ms = (time.monotonic() - start) * 1000
        return result

    async def _cleanup_conversations(self, before: datetime) -> tuple[int, int]:
        ids_result = await self._session.execute(
            text(
                "SELECT id FROM conversations "
                "WHERE state = ANY(:states) AND updated_at < :before "
                "LIMIT :limit"
            ),
            {
                "states": list(_TERMINAL_CONVERSATION_STATES),
                "before": before,
                "limit": _BATCH_SIZE,
            },
        )
        conv_ids = [row[0] for row in ids_result.all()]
        if not conv_ids:
            return 0, 0

        turn_result = await self._session.execute(
            text("DELETE FROM conversation_turns WHERE conversation_id = ANY(:ids)"),
            {"ids": conv_ids},
        )
        turns_deleted = turn_result.rowcount or 0  # type: ignore[attr-defined]

        conv_result = await self._session.execute(
            text("DELETE FROM conversations WHERE id = ANY(:ids)"),
            {"ids": conv_ids},
        )
        convs_deleted = conv_result.rowcount or 0  # type: ignore[attr-defined]

        logger.info(
            "retention_conversations_cleaned",
            extra={
                "conversations": convs_deleted,
                "turns": turns_deleted,
            },
        )
        return convs_deleted, turns_deleted

    async def _cleanup_notifications(
        self,
        delivered_before: datetime,
        dead_letter_before: datetime,
    ) -> int:
        delivered_result = await self._session.execute(
            text(
                "DELETE FROM notification_outbox "
                "WHERE status = 'delivered' AND updated_at < :before "
                "AND ctid = ANY(ARRAY("
                "  SELECT ctid FROM notification_outbox "
                "  WHERE status = 'delivered' AND updated_at < :before "
                "  LIMIT :limit"
                "))"
            ),
            {"before": delivered_before, "limit": _BATCH_SIZE},
        )
        delivered_count = delivered_result.rowcount or 0  # type: ignore[attr-defined]

        dead_result = await self._session.execute(
            text(
                "DELETE FROM notification_outbox "
                "WHERE status = 'dead_letter' AND updated_at < :before "
                "AND ctid = ANY(ARRAY("
                "  SELECT ctid FROM notification_outbox "
                "  WHERE status = 'dead_letter' AND updated_at < :before "
                "  LIMIT :limit"
                "))"
            ),
            {"before": dead_letter_before, "limit": _BATCH_SIZE},
        )
        dead_count = dead_result.rowcount or 0  # type: ignore[attr-defined]

        total = delivered_count + dead_count
        if total > 0:
            logger.info(
                "retention_notifications_cleaned",
                extra={
                    "delivered": delivered_count,
                    "dead_letter": dead_count,
                },
            )
        return total

    async def _cleanup_pending_actions(self, before: datetime) -> int:
        result = await self._session.execute(
            text(
                "DELETE FROM pending_actions "
                "WHERE status = ANY(:statuses) AND updated_at < :before "
                "AND committed_entity_id IS NOT NULL "
                "AND id NOT IN ("
                "  SELECT pending_action_id FROM appointments "
                "  WHERE pending_action_id IS NOT NULL"
                ") "
                "AND id NOT IN ("
                "  SELECT pending_action_id FROM appointment_commits"
                ") "
                "AND ctid = ANY(ARRAY("
                "  SELECT pa.ctid FROM pending_actions pa "
                "  WHERE pa.status = ANY(:statuses) AND pa.updated_at < :before "
                "  AND pa.committed_entity_id IS NOT NULL "
                "  AND pa.id NOT IN ("
                "    SELECT pending_action_id FROM appointments "
                "    WHERE pending_action_id IS NOT NULL"
                "  ) "
                "  AND pa.id NOT IN ("
                "    SELECT pending_action_id FROM appointment_commits"
                "  ) "
                "  LIMIT :limit"
                "))"
            ),
            {
                "statuses": list(_TERMINAL_PA_STATUSES),
                "before": before,
                "limit": _BATCH_SIZE,
            },
        )
        count = result.rowcount or 0  # type: ignore[attr-defined]
        if count > 0:
            logger.info(
                "retention_pending_actions_cleaned",
                extra={"count": count},
            )
        return count

    async def _cleanup_callbacks(self, before: datetime) -> int:
        """Sweep aged callback pending actions.

        A callback (action_type='callback') is a voice give-up follow-up record
        carrying caller phone + booking intent. It NEVER commits an entity, so
        the booking-PA sweep above — which requires committed_entity_id IS NOT
        NULL to protect committed bookings — can never reach it, leaving it
        structurally immortal. This branch deletes callbacks past their horizon
        WITHOUT that gate (callbacks legitimately have no committed entity),
        which is the correct rule for this row type and only this row type. The
        booking-PA sweep is deliberately left untouched: its gate is right for
        bookings.

        No committed-booking cross-checks are needed here because a callback is
        never referenced by an appointment or an appointment_commit — it exists
        precisely because no appointment was created.
        """
        result = await self._session.execute(
            text(
                "DELETE FROM pending_actions "
                "WHERE action_type = 'callback' AND updated_at < :before "
                "AND ctid = ANY(ARRAY("
                "  SELECT pa.ctid FROM pending_actions pa "
                "  WHERE pa.action_type = 'callback' AND pa.updated_at < :before "
                "  LIMIT :limit"
                "))"
            ),
            {"before": before, "limit": _BATCH_SIZE},
        )
        count = result.rowcount or 0  # type: ignore[attr-defined]
        if count > 0:
            logger.info("retention_callbacks_cleaned", extra={"count": count})
        return count

    async def _cleanup_inbound_events(
        self,
        completed_before: datetime,
        dead_letter_before: datetime,
    ) -> int:
        completed_result = await self._session.execute(
            text(
                "DELETE FROM whatsapp_inbound_events "
                "WHERE status = 'completed' AND completed_at < :before "
                "AND ctid = ANY(ARRAY("
                "  SELECT ctid FROM whatsapp_inbound_events "
                "  WHERE status = 'completed' AND completed_at < :before "
                "  LIMIT :limit"
                "))"
            ),
            {"before": completed_before, "limit": _BATCH_SIZE},
        )
        completed_count = completed_result.rowcount or 0  # type: ignore[attr-defined]

        dead_result = await self._session.execute(
            text(
                "DELETE FROM whatsapp_inbound_events "
                "WHERE status IN ('dead_letter', 'response_failed') "
                "AND dead_lettered_at < :before "
                "AND ctid = ANY(ARRAY("
                "  SELECT ctid FROM whatsapp_inbound_events "
                "  WHERE status IN ('dead_letter', 'response_failed') "
                "  AND dead_lettered_at < :before "
                "  LIMIT :limit"
                "))"
            ),
            {"before": dead_letter_before, "limit": _BATCH_SIZE},
        )
        dead_count = dead_result.rowcount or 0  # type: ignore[attr-defined]

        total = completed_count + dead_count
        if total > 0:
            logger.info(
                "retention_inbound_events_cleaned",
                extra={"completed": completed_count, "dead_letter": dead_count},
            )
        return total

    async def _redact_call_transcripts(self, before: datetime) -> int:
        """Strip the spoken transcript from expired calls, keeping the call row.

        An UPDATE and not a DELETE, unlike everything else here. The call row
        is operational evidence -- duration, outcome, which clinic -- and none
        of it is anything the patient said. The transcript is the part that is
        their words, so it is the part that expires.

        Two details that would each be a quiet hole:

        `COALESCE(ended_at, started_at)`, not `ended_at`. A call whose
        completed callback never arrived (carrier drop, our own crash) keeps
        ended_at NULL forever, and keying on ended_at alone would exempt
        exactly those rows from retention permanently -- the failure mode
        where a missing record reads as nothing to do.

        The transcript is replaced with a redaction marker rather than SQL
        NULL. NULL is indistinguishable from a call that never produced a
        transcript at all, so there would be no way to answer "was this
        purged?" with anything but a shrug. The marker carries no patient
        content and needs no migration -- it lives in the existing JSONB
        column, so purging stays available whatever the migration head is.
        """
        result = await self._session.execute(
            text(
                "UPDATE calls SET transcript = jsonb_build_object("
                "  'redacted', true,"
                "  'redacted_at', NOW(),"
                "  'policy', 'call_transcripts'"
                ") "
                "WHERE transcript IS NOT NULL "
                "AND (transcript->>'redacted') IS NULL "
                "AND COALESCE(ended_at, started_at) < :before "
                "AND ctid = ANY(ARRAY("
                "  SELECT c.ctid FROM calls c "
                "  WHERE c.transcript IS NOT NULL "
                "  AND (c.transcript->>'redacted') IS NULL "
                "  AND COALESCE(c.ended_at, c.started_at) < :before "
                "  LIMIT :limit"
                "))"
            ),
            {"before": before, "limit": _BATCH_SIZE},
        )
        count = result.rowcount or 0  # type: ignore[attr-defined]
        if count > 0:
            logger.info("retention_call_transcripts_redacted", extra={"count": count})
        return count
