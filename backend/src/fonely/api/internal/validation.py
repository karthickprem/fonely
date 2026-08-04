"""Internal appointment validation port implementation.

Resolves authoritative tenant-scoped facts and scheduling policy for every
proposal and confirmation.
"""

from datetime import UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.appointments.availability import derive_windows
from fonely.domain.appointments.validation import AppointmentValidationPort
from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.errors import PendingActionIdempotencyConflictError
from fonely.domain.pending_actions.payloads import (
    AppointmentFacts,
    CancelAppointmentData,
    CreateAppointmentData,
    PendingAppointmentEnvelope,
    RescheduleAppointmentData,
)
from fonely.domain.pending_actions.snapshots import payload_digest
from fonely.models.schema import Business, Resource, Service, ServiceResourceEligibility
from fonely.services.availability import AvailabilityReason, AvailabilityService


class AppointmentAvailabilityError(ValueError):
    def __init__(self, reason: AvailabilityReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class InternalValidationPort(AppointmentValidationPort):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._availability = AvailabilityService(session)

    async def _resolve_facts(
        self,
        business_id: int,
        stub_facts: AppointmentFacts,
        *,
        exclude_appointment_id: int | None = None,
    ) -> AppointmentFacts:
        business = (
            await self._session.execute(select(Business).where(Business.id == business_id))
        ).scalar_one_or_none()
        if business is None:
            raise ValueError("Business not found")

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

        eligibility = (
            await self._session.execute(
                select(ServiceResourceEligibility.id).where(
                    ServiceResourceEligibility.business_id == business_id,
                    ServiceResourceEligibility.service_id == stub_facts.service_id,
                    ServiceResourceEligibility.resource_id == stub_facts.resource_id,
                    ServiceResourceEligibility.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if eligibility is None:
            raise ValueError("Resource is not eligible for this service")

        decision = await self._availability.check_exact_slot(
            business_id,
            service.id,
            resource.id,
            stub_facts.start_at,
            exclude_appointment_id=exclude_appointment_id,
            alternative_limit=0,
        )
        if not decision.available:
            raise AppointmentAvailabilityError(decision.reason)

        start_at = stub_facts.start_at.astimezone(UTC)
        appointment, effective = derive_windows(
            start_at,
            duration_minutes=service.duration_minutes,
            buffer_before_minutes=service.buffer_before_minutes,
            buffer_after_minutes=service.buffer_after_minutes,
        )
        return AppointmentFacts(
            service_id=service.id,
            service_name=service.name,
            resource_id=resource.id,
            resource_name=resource.name,
            start_at=appointment.start_at,
            end_at=appointment.end_at,
            effective_start_at=effective.start_at,
            effective_end_at=effective.end_at,
            duration_minutes=service.duration_minutes,
            buffer_before_minutes=service.buffer_before_minutes,
            buffer_after_minutes=service.buffer_after_minutes,
            price=service.price,
            business_timezone=business.timezone,
        )

    async def validate_for_actor(
        self,
        actor: ActorContext,
        payload: PendingAppointmentEnvelope,
    ) -> PendingAppointmentEnvelope:
        if isinstance(payload.data, CancelAppointmentData):
            return payload
        if isinstance(payload.data, RescheduleAppointmentData):
            new_facts = await self._resolve_facts(
                actor.business_id,
                payload.data.new_facts,
                exclude_appointment_id=payload.data.target_appointment_id,
            )
            if new_facts == payload.data.old_facts:
                raise ValueError("Reschedule must change appointment facts")
            return PendingAppointmentEnvelope(
                data=RescheduleAppointmentData(
                    target_appointment_id=payload.data.target_appointment_id,
                    target_expected_version=payload.data.target_expected_version,
                    old_facts=payload.data.old_facts,
                    new_facts=new_facts,
                )
            )
        if not isinstance(payload.data, CreateAppointmentData):
            raise ValueError(f"Unsupported appointment operation: {type(payload.data).__name__}")
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
        if isinstance(payload.data, CancelAppointmentData):
            return payload
        if isinstance(payload.data, RescheduleAppointmentData):
            stored_facts = payload.data.new_facts
            current_facts = await self._resolve_facts(
                business_id,
                stored_facts,
                exclude_appointment_id=payload.data.target_appointment_id,
            )
            self._require_unchanged(stored_facts, current_facts)
            return payload
        if not isinstance(payload.data, CreateAppointmentData):
            raise ValueError(f"Unsupported appointment operation: {type(payload.data).__name__}")
        stored_facts = payload.data.facts
        current_facts = await self._resolve_facts(business_id, stored_facts)
        self._require_unchanged(stored_facts, current_facts)
        return payload

    def _require_unchanged(
        self, stored_facts: AppointmentFacts, current_facts: AppointmentFacts
    ) -> None:
        if current_facts != stored_facts:
            raise PendingActionIdempotencyConflictError(
                "Authoritative facts changed since proposal; revalidation required"
            )

    async def validate_idempotent_retry(
        self,
        actor: ActorContext,
        proposed: PendingAppointmentEnvelope,
        stored: PendingAppointmentEnvelope,
    ) -> None:
        if payload_digest(proposed) != payload_digest(stored):
            raise PendingActionIdempotencyConflictError(
                "Idempotency key already used for a different appointment request"
            )

    async def validate_completion_evidence(
        self,
        business_id: int,
        payload: PendingAppointmentEnvelope,
        committed_entity_type: str,
        committed_entity_id: int,
    ) -> None:
        pass  # PostgreSQL deferred constraints enforce completion evidence.
