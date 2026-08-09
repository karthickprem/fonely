"""Tenant-scoped durable owner command proposal persistence."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.schema import OwnerCommandProposal


class OwnerCommandProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_idempotent(self, values: Mapping[str, Any]) -> OwnerCommandProposal | None:
        statement = (
            pg_insert(OwnerCommandProposal)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_owner_proposal_idempotency")
            .returning(OwnerCommandProposal)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_id(
        self,
        business_id: int,
        proposal_id: str,
        *,
        for_update: bool = False,
    ) -> OwnerCommandProposal | None:
        statement = select(OwnerCommandProposal).where(
            OwnerCommandProposal.business_id == business_id,
            OwnerCommandProposal.id == proposal_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.scalars(statement)).one_or_none()

    async def get_by_idempotency_key(
        self,
        business_id: int,
        idempotency_key: str,
    ) -> OwnerCommandProposal | None:
        statement = select(OwnerCommandProposal).where(
            OwnerCommandProposal.business_id == business_id,
            OwnerCommandProposal.idempotency_key == idempotency_key,
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def get_latest_pending_for_owner(
        self,
        business_id: int,
        owner_user_id: int,
        owner_phone: str,
        *,
        for_update: bool = False,
    ) -> OwnerCommandProposal | None:
        return await self.get_latest_for_owner(
            business_id,
            owner_user_id,
            owner_phone,
            statuses=("pending_confirmation",),
            for_update=for_update,
        )

    async def get_latest_for_owner(
        self,
        business_id: int,
        owner_user_id: int,
        owner_phone: str,
        *,
        statuses: tuple[str, ...],
        for_update: bool = False,
    ) -> OwnerCommandProposal | None:
        statement = (
            select(OwnerCommandProposal)
            .where(
                OwnerCommandProposal.business_id == business_id,
                OwnerCommandProposal.owner_user_id == owner_user_id,
                OwnerCommandProposal.owner_phone_snapshot == owner_phone,
                OwnerCommandProposal.status.in_(statuses),
            )
            .order_by(OwnerCommandProposal.created_at.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.scalars(statement)).one_or_none()

    async def transition_status(
        self,
        business_id: int,
        proposal_id: str,
        expected_version: int,
        expected_status: str,
        new_status: str,
        **updates: Any,
    ) -> OwnerCommandProposal | None:
        statement = (
            update(OwnerCommandProposal)
            .where(
                OwnerCommandProposal.business_id == business_id,
                OwnerCommandProposal.id == proposal_id,
                OwnerCommandProposal.expected_version == expected_version,
                OwnerCommandProposal.status == expected_status,
            )
            .values(
                status=new_status,
                expected_version=OwnerCommandProposal.expected_version + 1,
                updated_at=func.now(),
                **updates,
            )
            .returning(OwnerCommandProposal)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def expire_pending(
        self,
        business_id: int,
        owner_user_id: int,
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(UTC)
        statement = (
            update(OwnerCommandProposal)
            .where(
                OwnerCommandProposal.business_id == business_id,
                OwnerCommandProposal.owner_user_id == owner_user_id,
                OwnerCommandProposal.status == "pending_confirmation",
                OwnerCommandProposal.expires_at <= current,
            )
            .values(
                status="expired",
                expected_version=OwnerCommandProposal.expected_version + 1,
                updated_at=func.now(),
            )
        )
        result = await self._session.execute(statement)
        return int(getattr(result, "rowcount", 0))
