"""Booking orchestrator — bridges AvailabilityService and offer management.

Called by ConversationService to produce typed offers and validate
selections. Does not own the conversation state machine.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.booking.contract import AvailabilityOffer, SelectedSlot
from fonely.domain.booking.offers import (
    OfferValidationError,
    build_offer,
    deserialize_offer,
    serialize_offer,
    validate_selection,
)
from fonely.services.availability import AvailabilityService


class BookingOrchestrator:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._avail = AvailabilityService(session)

    async def check_and_offer(
        self,
        *,
        business_id: int,
        conversation_id: str,
        service_id: int,
        service_name: str,
        resource_id: int,
        resource_name: str,
        requested_start: datetime,
        business_timezone: str,
        exclude_appointment_id: int | None = None,
    ) -> tuple[bool, AvailabilityOffer | None]:
        """Check exact slot and build an offer from alternatives if unavailable.

        Returns (exact_available, offer_or_none).
        If exact_available is True, offer contains only the requested slot.
        If False, offer contains alternative slots (may be empty).
        """
        decision = await self._avail.check_exact_slot(
            business_id,
            service_id,
            resource_id,
            requested_start,
            exclude_appointment_id=exclude_appointment_id,
        )

        if decision.available:
            tz = ZoneInfo(business_timezone)
            exact_slots = await self._avail.get_available_slots(
                business_id,
                service_id,
                resource_id,
                requested_start.astimezone(tz).date(),
                exclude_appointment_id=exclude_appointment_id,
            )
            matching = [s for s in exact_slots if s.start_at == requested_start]
            if not matching:
                return False, None

            raw: list[dict[str, object]] = [
                {"start_at": matching[0].start_at, "end_at": matching[0].end_at}
            ]
            offer = build_offer(
                business_id=business_id,
                conversation_id=conversation_id,
                service_id=service_id,
                service_name=service_name,
                resource_id=resource_id,
                resource_name=resource_name,
                target_date=requested_start.astimezone(tz).date().isoformat(),
                available_slots=raw,
                business_timezone=business_timezone,
            )
            return True, offer

        if not decision.alternatives:
            return False, None

        raw_alts: list[dict[str, object]] = [
            {"start_at": s.start_at, "end_at": s.end_at} for s in decision.alternatives
        ]
        tz = ZoneInfo(business_timezone)
        offer = build_offer(
            business_id=business_id,
            conversation_id=conversation_id,
            service_id=service_id,
            service_name=service_name,
            resource_id=resource_id,
            resource_name=resource_name,
            target_date=requested_start.astimezone(tz).date().isoformat(),
            available_slots=raw_alts,
            business_timezone=business_timezone,
        )
        return False, offer

    def validate_token_selection(
        self,
        offer_data: dict[str, object],
        token: str,
        *,
        business_id: int,
        conversation_id: str,
    ) -> SelectedSlot:
        offer = deserialize_offer(offer_data)
        if offer is None:
            raise OfferValidationError("malformed_offer", "Cannot deserialize offer")
        return validate_selection(
            offer, token, business_id=business_id, conversation_id=conversation_id
        )

    @staticmethod
    def serialize(offer: AvailabilityOffer) -> dict[str, object]:
        return serialize_offer(offer)

    @staticmethod
    def deserialize(data: dict[str, object]) -> AvailabilityOffer | None:
        return deserialize_offer(data)
