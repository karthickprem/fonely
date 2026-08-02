"""Tenant-scoped onboarding persistence within a caller-owned transaction."""

from collections.abc import Mapping
from datetime import time
from typing import Any

from sqlalchemy import func, select, update
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

    async def upsert_schedule(
        self,
        business_id: int,
        resource_id: int | None,
        day_of_week: int,
        open_time: time,
        close_time: time,
    ) -> OperatingSchedule:
        values: dict[str, Any] = {
            "business_id": business_id,
            "resource_id": resource_id,
            "day_of_week": day_of_week,
            "open_time": open_time,
            "close_time": close_time,
            "is_active": True,
        }
        schedule = OperatingSchedule(**values)
        self._session.add(schedule)
        await self._session.flush()
        return schedule

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
        exc = ScheduleException(
            business_id=business_id,
            resource_id=resource_id,
            exception_date=exception_date,
            is_closed=is_closed,
            open_time=open_time,
            close_time=close_time,
            reason=reason,
        )
        self._session.add(exc)
        await self._session.flush()
        return exc
