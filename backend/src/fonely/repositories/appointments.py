"""Tenant-scoped appointment and resource-allocation repository."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.schema import Appointment, AppointmentCommit, ResourceAllocation


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

    async def get_by_business_and_id(
        self,
        business_id: int,
        appointment_id: int,
    ) -> Appointment | None:
        result = await self._session.execute(
            select(Appointment).where(
                Appointment.business_id == business_id,
                Appointment.id == appointment_id,
            )
        )
        return result.scalar_one_or_none()

    async def lock_appointment(
        self,
        business_id: int,
        appointment_id: int,
    ) -> Appointment | None:
        result = await self._session.execute(
            select(Appointment)
            .where(
                Appointment.business_id == business_id,
                Appointment.id == appointment_id,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def update_appointment(
        self,
        business_id: int,
        appointment_id: int,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> Appointment | None:
        stmt = (
            update(Appointment)
            .where(
                Appointment.business_id == business_id,
                Appointment.id == appointment_id,
                Appointment.version == expected_version,
            )
            .values(**values, version=expected_version + 1)
            .returning(Appointment)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            await self._session.flush()
        return row

    async def update_allocation_status(
        self,
        business_id: int,
        appointment_id: int,
        new_status: str,
    ) -> None:
        await self._session.execute(
            update(ResourceAllocation)
            .where(
                ResourceAllocation.business_id == business_id,
                ResourceAllocation.appointment_id == appointment_id,
                ResourceAllocation.status == "active",
            )
            .values(
                status=new_status,
                version=ResourceAllocation.version + 1,
            )
        )
        await self._session.flush()

    async def insert_commit(self, values: Mapping[str, Any]) -> AppointmentCommit:
        commit = AppointmentCommit(**values)
        self._session.add(commit)
        await self._session.flush()
        return commit

    async def lock_resource_schedule(
        self,
        business_id: int,
        resource_id: int,
    ) -> None:
        await self._session.execute(
            text("SELECT 1 FROM resources WHERE business_id = :bid AND id = :rid FOR UPDATE"),
            {"bid": business_id, "rid": resource_id},
        )
