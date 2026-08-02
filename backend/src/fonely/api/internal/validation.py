"""Internal appointment validation port implementation.

Resolves authoritative tenant-scoped facts from the database for the internal
text appointment slice. Production channels will use richer validation.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.appointments.validation import AppointmentValidationPort
from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.payloads import (
    AppointmentFacts,
    CreateAppointmentData,
    PendingAppointmentEnvelope,
)
from fonely.models.schema import Resource, Service


class InternalValidationPort(AppointmentValidationPort):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def validate_for_actor(
        self,
        actor: ActorContext,
        payload: PendingAppointmentEnvelope,
    ) -> PendingAppointmentEnvelope:
        assert isinstance(payload.data, CreateAppointmentData)
        stub_facts = payload.data.facts

        service = (
            await self._session.execute(
                select(Service).where(
                    Service.business_id == actor.business_id,
                    Service.id == stub_facts.service_id,
                    Service.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if service is None:
            raise ValueError("Service not found or inactive")

        resource = (
            await self._session.execute(
                select(Resource).where(
                    Resource.business_id == actor.business_id,
                    Resource.id == stub_facts.resource_id,
                    Resource.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if resource is None:
            raise ValueError("Resource not found or inactive")

        start_at = stub_facts.start_at
        end_at = start_at + timedelta(minutes=service.duration_minutes)
        buffer_before = getattr(service, "buffer_before_minutes", 0) or 0
        buffer_after = getattr(service, "buffer_after_minutes", 0) or 0
        effective_start = start_at - timedelta(minutes=buffer_before)
        effective_end = end_at + timedelta(minutes=buffer_after)

        from fonely.models.schema import Business

        business = (
            await self._session.execute(select(Business).where(Business.id == actor.business_id))
        ).scalar_one_or_none()
        timezone = business.timezone if business else "Asia/Kolkata"

        resolved_facts = AppointmentFacts(
            service_id=service.id,
            service_name=service.name,
            resource_id=resource.id,
            resource_name=resource.name,
            start_at=start_at,
            end_at=end_at,
            effective_start_at=effective_start,
            effective_end_at=effective_end,
            duration_minutes=service.duration_minutes,
            buffer_before_minutes=buffer_before,
            buffer_after_minutes=buffer_after,
            price=service.price,
            business_timezone=timezone,
        )

        return PendingAppointmentEnvelope(
            data=CreateAppointmentData(
                facts=resolved_facts,
                customer_name=payload.data.customer_name,
                customer_phone=payload.data.customer_phone,
                reason=payload.data.reason,
                call_id=payload.data.call_id,
            )
        )

    async def validate_stored(
        self,
        business_id: int,
        payload: PendingAppointmentEnvelope,
    ) -> PendingAppointmentEnvelope:
        return payload

    async def validate_idempotent_retry(
        self,
        actor: ActorContext,
        proposed: PendingAppointmentEnvelope,
        stored: PendingAppointmentEnvelope,
    ) -> None:
        pass

    async def validate_completion_evidence(
        self,
        business_id: int,
        payload: PendingAppointmentEnvelope,
        committed_entity_type: str,
        committed_entity_id: int,
    ) -> None:
        pass
