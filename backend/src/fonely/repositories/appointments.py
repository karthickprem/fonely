"""Tenant-scoped appointment and resource-allocation repository."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.schema import Appointment, ResourceAllocation


class AppointmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_business_and_pending_action(
        self,
        business_id: int,
        pending_action_id: int,
    ) -> Appointment | None:
        result = await self._session.execute(
            select(Appointment)
            .where(
                Appointment.business_id == business_id,
                Appointment.pending_action_id == pending_action_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def insert(self, values: Mapping[str, Any]) -> Appointment:
        appointment = Appointment(**values)
        self._session.add(appointment)
        await self._session.flush()
        return appointment

    async def insert_allocation(self, values: Mapping[str, Any]) -> ResourceAllocation:
        allocation = ResourceAllocation(**values)
        self._session.add(allocation)
        await self._session.flush()
        return allocation

    async def force_constraints(self, constraint_sql: str) -> None:
        await self._session.execute(text(constraint_sql))

    async def lock_resource_schedule(
        self,
        business_id: int,
        resource_id: int,
    ) -> None:
        await self._session.execute(
            text("SELECT 1 FROM resources WHERE business_id = :bid AND id = :rid FOR UPDATE"),
            {"bid": business_id, "rid": resource_id},
        )
