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
    execution_time_ms: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "conversations_deleted": self.conversations_deleted,
            "turns_deleted": self.turns_deleted,
            "notifications_deleted": self.notifications_deleted,
            "pending_actions_deleted": self.pending_actions_deleted,
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

        result.execution_time_ms = (time.monotonic() - start) * 1000
        return result

    async def _cleanup_conversations(self, before: datetime) -> tuple[int, int]:
        ids_result = await self._session.execute(
            text(
                "SELECT id FROM conversations "
                "WHERE state IN :states AND updated_at < :before "
                "LIMIT :limit"
            ),
            {
                "states": _TERMINAL_CONVERSATION_STATES,
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
        turns_deleted = turn_result.rowcount or 0

        conv_result = await self._session.execute(
            text("DELETE FROM conversations WHERE id = ANY(:ids)"),
            {"ids": conv_ids},
        )
        convs_deleted = conv_result.rowcount or 0

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
        delivered_count = delivered_result.rowcount or 0

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
        dead_count = dead_result.rowcount or 0

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
                "WHERE status IN :statuses AND updated_at < :before "
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
                "  WHERE pa.status IN :statuses AND pa.updated_at < :before "
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
                "statuses": _TERMINAL_PA_STATUSES,
                "before": before,
                "limit": _BATCH_SIZE,
            },
        )
        count = result.rowcount or 0
        if count > 0:
            logger.info(
                "retention_pending_actions_cleaned",
                extra={"count": count},
            )
        return count
