"""Tenant-scoped persistence operations for PendingAction."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.enums import PendingActionStatus, PendingActionType
from fonely.models.schema import PendingAction


class PendingActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, business_id: int, action_id: int) -> PendingAction | None:
        statement = (
            select(PendingAction)
            .where(
                PendingAction.business_id == business_id,
                PendingAction.id == action_id,
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_idempotency_key(
        self,
        business_id: int,
        key: str,
    ) -> PendingAction | None:
        statement = (
            select(PendingAction)
            .where(
                PendingAction.business_id == business_id,
                PendingAction.idempotency_key == key,
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_active_for_session(
        self,
        business_id: int,
        session_id: str,
        now: datetime,
        action_type: PendingActionType | None = None,
    ) -> PendingAction | None:
        expirable = (
            PendingActionStatus.COLLECTING_DETAILS.value,
            PendingActionStatus.AWAITING_CONFIRMATION.value,
        )
        active_condition = (PendingAction.status == PendingActionStatus.COMMITTING.value) | (
            PendingAction.status.in_(expirable) & (PendingAction.expires_at > now)
        )
        statement = select(PendingAction).where(
            PendingAction.business_id == business_id,
            PendingAction.session_id == session_id,
            active_condition,
        )
        if action_type is not None:
            statement = statement.where(PendingAction.action_type == action_type.value)
        statement = (
            statement.order_by(PendingAction.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_pending_by_type(
        self,
        business_id: int,
        action_type: PendingActionType,
        statuses: tuple[str, ...],
        *,
        limit: int = 100,
    ) -> list[PendingAction]:
        """Tenant-scoped list of pending actions of one type in the given
        statuses, newest first. Tenant isolation is not optional: the
        business_id predicate is the ONLY thing that keeps one clinic's callbacks
        out of another's worklist, so it is always applied and never derived from
        anything the caller supplied beyond the trusted business_id.
        """
        statement = (
            select(PendingAction)
            .where(
                PendingAction.business_id == business_id,
                PendingAction.action_type == action_type.value,
                PendingAction.status.in_(statuses),
            )
            .order_by(PendingAction.created_at.desc(), PendingAction.id.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def insert_idempotent(
        self,
        values: Mapping[str, Any],
    ) -> PendingAction | None:
        statement = (
            pg_insert(PendingAction)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[PendingAction.business_id, PendingAction.idempotency_key]
            )
            .returning(PendingAction)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def conditional_update(
        self,
        *,
        business_id: int,
        action_id: int,
        expected_version: int,
        expected_status: PendingActionStatus,
        values: Mapping[str, Any],
        expires_after: datetime | None = None,
    ) -> PendingAction | None:
        criteria = [
            PendingAction.business_id == business_id,
            PendingAction.id == action_id,
            PendingAction.version == expected_version,
            PendingAction.status == expected_status.value,
        ]
        if expires_after is not None:
            criteria.append(PendingAction.expires_at > expires_after)
        statement = (
            update(PendingAction)
            .where(*criteria)
            .values(**values, version=PendingAction.version + 1)
            .returning(PendingAction)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def bulk_expire(
        self,
        *,
        now: datetime,
        batch_size: int,
    ) -> tuple[int, ...]:
        eligible = (
            PendingActionStatus.COLLECTING_DETAILS.value,
            PendingActionStatus.AWAITING_CONFIRMATION.value,
        )
        ids_statement = (
            select(PendingAction.id)
            .where(
                PendingAction.status.in_(eligible),
                PendingAction.expires_at <= now,
            )
            .order_by(PendingAction.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        ids = tuple((await self._session.scalars(ids_statement)).all())
        if not ids:
            return ()
        update_statement = (
            update(PendingAction)
            .where(
                PendingAction.id.in_(ids),
                PendingAction.status.in_(eligible),
                PendingAction.expires_at <= now,
            )
            .values(
                status=PendingActionStatus.EXPIRED.value,
                version=PendingAction.version + 1,
            )
            .returning(PendingAction.id)
        )
        return tuple((await self._session.scalars(update_statement)).all())
