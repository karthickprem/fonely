"""Tenant-scoped onboarding persistence within a caller-owned transaction."""

from collections.abc import Mapping
from datetime import time
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.schema import (
    BusinessConfigurationCommit,
    BusinessOnboardingDraft,
    OperatingSchedule,
    Resource,
    ScheduleException,
    Service,
    ServiceResourceEligibility,
)


class OnboardingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_draft(self, business_id: int, draft_id: int) -> BusinessOnboardingDraft | None:
        statement = (
            select(BusinessOnboardingDraft)
            .where(
                BusinessOnboardingDraft.business_id == business_id,
                BusinessOnboardingDraft.id == draft_id,
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def get_draft_by_digest(
        self, business_id: int, digest: str
    ) -> BusinessOnboardingDraft | None:
        statement = (
            select(BusinessOnboardingDraft)
            .where(
                BusinessOnboardingDraft.business_id == business_id,
                BusinessOnboardingDraft.draft_digest == digest,
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def insert_draft(self, values: Mapping[str, Any]) -> BusinessOnboardingDraft:
        draft = BusinessOnboardingDraft(**values)
        self._session.add(draft)
        await self._session.flush()
        return draft

    async def update_draft_status(
        self,
        draft_id: int,
        business_id: int,
        expected_version: int,
        **updates: Any,
    ) -> BusinessOnboardingDraft | None:
        statement = (
            update(BusinessOnboardingDraft)
            .where(
                BusinessOnboardingDraft.id == draft_id,
                BusinessOnboardingDraft.business_id == business_id,
                BusinessOnboardingDraft.version == expected_version,
            )
            .values(**updates, version=BusinessOnboardingDraft.version + 1, updated_at=func.now())
            .returning(BusinessOnboardingDraft)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def insert_commit(self, values: Mapping[str, Any]) -> BusinessConfigurationCommit:
        commit = BusinessConfigurationCommit(**values)
        self._session.add(commit)
        await self._session.flush()
        return commit

    async def upsert_service(self, business_id: int, name: str, **fields: Any) -> Service:
        statement = (
            pg_insert(Service)
            .values(business_id=business_id, name=name, **fields)
            .on_conflict_do_update(
                index_elements=[Service.business_id, Service.name],
                set_={k: v for k, v in fields.items()},
            )
            .returning(Service)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def upsert_resource(self, business_id: int, name: str, **fields: Any) -> Resource:
        statement = (
            pg_insert(Resource)
            .values(business_id=business_id, name=name, **fields)
            .on_conflict_do_update(
                index_elements=[Resource.business_id, Resource.name],
                set_={k: v for k, v in fields.items()},
            )
            .returning(Resource)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def upsert_eligibility(
        self, business_id: int, service_id: int, resource_id: int
    ) -> ServiceResourceEligibility:
        statement = (
            pg_insert(ServiceResourceEligibility)
            .values(
                business_id=business_id,
                service_id=service_id,
                resource_id=resource_id,
                is_active=True,
            )
            .on_conflict_do_update(
                constraint="uq_service_resource_eligibility",
                set_={"is_active": True, "updated_at": func.now()},
            )
            .returning(ServiceResourceEligibility)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def deactivate_schedules(self, business_id: int) -> int:
        """Retire the tenant's whole timetable so activation can restate it.

        Activation declares what the clinic's hours *are*, not what to add to
        them, so a day the owner dropped — or a doctor no longer in the draft
        — has to stop being open. Rows are deactivated rather than deleted:
        availability already filters on `is_active`, ids survive, and the
        previous timetable stays readable for anyone auditing what the clinic
        used to do. The caller re-upserts every opening the draft still
        declares, which flips those rows back to active in the same
        transaction.
        """
        statement = (
            update(OperatingSchedule)
            .where(
                OperatingSchedule.business_id == business_id,
                OperatingSchedule.is_active.is_(True),
            )
            .values(is_active=False)
        )
        return int((await self._session.execute(statement)).rowcount or 0)

    async def upsert_schedule(
        self,
        business_id: int,
        resource_id: int | None,
        day_of_week: int,
        open_time: time,
        close_time: time,
    ) -> OperatingSchedule:
        """Declare one opening, replacing any existing one that starts then.

        Business-level and resource-level openings live under two different
        partial unique indexes, so the conflict target has to name the same
        predicate PostgreSQL used to build the index.
        """
        index_where = (
            OperatingSchedule.resource_id.is_(None)
            if resource_id is None
            else OperatingSchedule.resource_id.is_not(None)
        )
        index_elements = (
            [
                OperatingSchedule.business_id,
                OperatingSchedule.day_of_week,
                OperatingSchedule.open_time,
            ]
            if resource_id is None
            else [
                OperatingSchedule.business_id,
                OperatingSchedule.resource_id,
                OperatingSchedule.day_of_week,
                OperatingSchedule.open_time,
            ]
        )
        statement = (
            pg_insert(OperatingSchedule)
            .values(
                business_id=business_id,
                resource_id=resource_id,
                day_of_week=day_of_week,
                open_time=open_time,
                close_time=close_time,
                is_active=True,
            )
            .on_conflict_do_update(
                index_elements=index_elements,
                index_where=index_where,
                set_={"close_time": close_time, "is_active": True},
            )
            .returning(OperatingSchedule)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def delete_exceptions(self, business_id: int) -> int:
        """Drop the tenant's exceptions so a cancelled holiday really is gone.

        Exceptions carry no `is_active` column, and a withdrawn closure has to
        stop suppressing bookings, so these are removed outright rather than
        retired. Nothing references them.
        """
        statement = delete(ScheduleException).where(
            ScheduleException.business_id == business_id,
        )
        return int((await self._session.execute(statement)).rowcount or 0)

    async def upsert_exception(
        self,
        business_id: int,
        resource_id: int | None,
        exception_date: Any,
        is_closed: bool = True,
        open_time: time | None = None,
        close_time: time | None = None,
        reason: str | None = None,
    ) -> ScheduleException:
        """Declare one dated exception, replacing any existing one that day."""
        index_where = (
            ScheduleException.resource_id.is_(None)
            if resource_id is None
            else ScheduleException.resource_id.is_not(None)
        )
        index_elements = (
            [ScheduleException.business_id, ScheduleException.exception_date]
            if resource_id is None
            else [
                ScheduleException.business_id,
                ScheduleException.resource_id,
                ScheduleException.exception_date,
            ]
        )
        statement = (
            pg_insert(ScheduleException)
            .values(
                business_id=business_id,
                resource_id=resource_id,
                exception_date=exception_date,
                is_closed=is_closed,
                open_time=open_time,
                close_time=close_time,
                reason=reason,
            )
            .on_conflict_do_update(
                index_elements=index_elements,
                index_where=index_where,
                set_={
                    "is_closed": is_closed,
                    "open_time": open_time,
                    "close_time": close_time,
                    "reason": reason,
                },
            )
            .returning(ScheduleException)
        )
        return (await self._session.execute(statement)).scalar_one()
