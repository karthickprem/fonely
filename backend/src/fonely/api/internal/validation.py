"""Internal appointment validation port implementation.

Resolves authoritative tenant-scoped facts from the database for the internal
text appointment slice. Production channels will use richer validation.
"""

from datetime import UTC, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.appointments.validation import AppointmentValidationPort
from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.payloads import (
    AppointmentFacts,
    CreateAppointmentData,
    PendingAppointmentEnvelope,
)
from fonely.models.schema import Business, Resource, Service


class InternalValidationPort(AppointmentValidationPort):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _resolve_facts(
        self,
        business_id: int,
        stub_facts: AppointmentFacts,
    ) -> AppointmentFacts:
        service = (
            await self._session.execute(
                select(Service).where(
                    Service.business_id == business_id,
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
                    Resource.business_id == business_id,
                    Resource.id == stub_facts.resource_id,
                    Resource.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if resource is None:
            raise ValueError("Resource not found or inactive")

        start_at = stub_facts.start_at.astimezone(UTC)
        end_at = start_at + timedelta(minutes=service.duration_minutes)
        buffer_before = getattr(service, "buffer_before_minutes", 0) or 0
        buffer_after = getattr(service, "buffer_after_minutes", 0) or 0
        effective_start = start_at - timedelta(minutes=buffer_before)
        effective_end = end_at + timedelta(minutes=buffer_after)

        business = (
            await self._session.execute(select(Business).where(Business.id == business_id))
        ).scalar_one_or_none()
        timezone = business.timezone if business else "Asia/Kolkata"

        return AppointmentFacts(
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

    async def validate_for_actor(
        self,
        actor: ActorContext,
        payload: PendingAppointmentEnvelope,
    ) -> PendingAppointmentEnvelope:
        assert isinstance(payload.data, CreateAppointmentData)
        facts = await self._resolve_facts(actor.business_id, payload.data.facts)
        return PendingAppointmentEnvelope(
            data=CreateAppointmentData(
                facts=facts,
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
        assert isinstance(payload.data, CreateAppointmentData)
        stored_facts = payload.data.facts
        current_facts = await self._resolve_facts(business_id, stored_facts)

        if (
            current_facts.service_id != stored_facts.service_id
            or current_facts.resource_id != stored_facts.resource_id
            or current_facts.duration_minutes != stored_facts.duration_minutes
            or current_facts.buffer_before_minutes != stored_facts.buffer_before_minutes
            or current_facts.buffer_after_minutes != stored_facts.buffer_after_minutes
            or current_facts.price != stored_facts.price
            or current_facts.business_timezone != stored_facts.business_timezone
            or current_facts.service_name != stored_facts.service_name
            or current_facts.resource_name != stored_facts.resource_name
        ):
            from fonely.domain.pending_actions.errors import (
                PendingActionIdempotencyConflictError,
            )

            raise PendingActionIdempotencyConflictError(
                "Authoritative facts changed since proposal; revalidation required"
            )

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
