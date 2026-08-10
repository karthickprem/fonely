"""Tenant-scoped owner command proposal persistence."""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from fonely.models.schema import OwnerCommandProposal

logger = logging.getLogger("fonely.repositories.owner_command_proposals")


class OwnerCommandProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_idempotent(self, values: dict[str, Any]) -> OwnerCommandProposal | None:
        """Insert a new proposal, handling both unique constraints.

        The INSERT uses ON CONFLICT DO NOTHING for the partial unique index
        (one pending per owner). However, the idempotency_key unique constraint
        ``uq_owner_proposal_idempotency`` can also fire when the same payload
        is submitted and a terminal proposal already exists. We catch that
        IntegrityError and return None so the service layer can look up the
        terminal replay.
        """
        stmt = (
            pg_insert(OwnerCommandProposal)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["business_id", "owner_user_id"],
                index_where=(OwnerCommandProposal.status == "pending_confirmation"),
            )
            .returning(OwnerCommandProposal)
        )
        try:
            async with self._session.begin_nested():
                return (await self._session.execute(stmt)).scalar_one_or_none()
        except IntegrityError as exc:
            if "uq_owner_proposal_idempotency" in str(exc):
                return None
            raise

    async def get_by_id(self, business_id: int, proposal_id: str) -> OwnerCommandProposal | None:
        stmt = (
            select(OwnerCommandProposal)
            .where(
                OwnerCommandProposal.business_id == business_id,
                OwnerCommandProposal.id == proposal_id,
            )
            .with_for_update()
        )
        return (await self._session.scalars(stmt)).first()

    async def get_by_idempotency_key(
        self, business_id: int, idempotency_key: str
    ) -> OwnerCommandProposal | None:
        stmt = select(OwnerCommandProposal).where(
            OwnerCommandProposal.business_id == business_id,
            OwnerCommandProposal.idempotency_key == idempotency_key,
        )
        return (await self._session.scalars(stmt)).first()

    async def count_by_key_prefix(self, business_id: int, key_prefix: str) -> int:
        stmt = select(func.count()).where(
            OwnerCommandProposal.business_id == business_id,
            OwnerCommandProposal.idempotency_key.startswith(key_prefix),
        )
        return (await self._session.scalar(stmt)) or 0

    async def find_completed_by_key_prefix(
        self, business_id: int, key_prefix: str
    ) -> OwnerCommandProposal | None:
        stmt = (
            select(OwnerCommandProposal)
            .where(
                OwnerCommandProposal.business_id == business_id,
                OwnerCommandProposal.idempotency_key.startswith(key_prefix),
                OwnerCommandProposal.status == "completed",
            )
            .limit(1)
        )
        return (await self._session.scalars(stmt)).first()

    async def get_latest_for_owner(
        self,
        business_id: int,
        owner_user_id: int,
        *,
        statuses: tuple[str, ...] = ("pending_confirmation",),
    ) -> OwnerCommandProposal | None:
        stmt = (
            select(OwnerCommandProposal)
            .where(
                OwnerCommandProposal.business_id == business_id,
                OwnerCommandProposal.owner_user_id == owner_user_id,
                OwnerCommandProposal.status.in_(statuses),
            )
            .order_by(OwnerCommandProposal.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        return (await self._session.scalars(stmt)).first()

    async def transition_status(
        self,
        proposal_id: str,
        business_id: int,
        expected_version: int,
        new_status: str,
        *,
        require_unexpired_at: datetime | None = None,
        **extra: Any,
    ) -> OwnerCommandProposal | None:
        conditions = [
            OwnerCommandProposal.id == proposal_id,
            OwnerCommandProposal.business_id == business_id,
            OwnerCommandProposal.expected_version == expected_version,
        ]
        if require_unexpired_at is not None:
            conditions.append(OwnerCommandProposal.expires_at > require_unexpired_at)

        values: dict[str, Any] = {
            "status": new_status,
            "expected_version": expected_version + 1,
            "updated_at": func.now(),
            **extra,
        }
        stmt = (
            update(OwnerCommandProposal)
            .where(*conditions)
            .values(**values)
            .returning(OwnerCommandProposal)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_business(
        self,
        business_id: int,
        *,
        statuses: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[OwnerCommandProposal]:
        stmt = select(OwnerCommandProposal).where(OwnerCommandProposal.business_id == business_id)
        if statuses:
            stmt = stmt.where(OwnerCommandProposal.status.in_(statuses))
        stmt = stmt.order_by(OwnerCommandProposal.created_at.desc()).limit(limit)
        result = (await self._session.scalars(stmt)).all()
        return list(result)
