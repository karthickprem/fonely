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
from fonely.services.availability import AvailabilityService, AvailableSlot


class BookingOrchestrator:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._avail = AvailabilityService(session)

    async def _exact_match(
        self,
        *,
        business_id: int,
        service_id: int,
        resource_id: int,
        requested_start: datetime,
        tz: ZoneInfo,
        exclude_appointment_id: int | None,
    ) -> tuple[AvailableSlot, ...]:
        """Return the exact slot at requested_start if bookable, else ()."""
        decision = await self._avail.check_exact_slot(
            business_id,
            service_id,
            resource_id,
            requested_start,
            exclude_appointment_id=exclude_appointment_id,
        )
        if not decision.available:
            return ()
        exact_slots = await self._avail.get_available_slots(
            business_id,
            service_id,
            resource_id,
            requested_start.astimezone(tz).date(),
            exclude_appointment_id=exclude_appointment_id,
        )
        return tuple(s for s in exact_slots if s.start_at == requested_start)

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
        alt_reading_start: datetime | None = None,
    ) -> tuple[bool, AvailabilityOffer | None]:
        """Check exact slot and build an offer from alternatives if unavailable.

        `alt_reading_start` is the SECOND meridiem reading of a bare time the
        patient did not disambiguate (e.g. "6" -> 06:00 primary, 18:00 alt).
        When given, both readings are considered so a Tamil-speaking patient who
        means 6 PM is not offered only morning slots: the reading that is open
        wins, and if both are open the alternatives span both so the patient's
        modulo-12 reply resolves cleanly.

        Returns (exact_available, offer_or_none).
        """
        tz = ZoneInfo(business_timezone)

        # Exact match on the primary reading -> confirm it.
        primary = await self._exact_match(
            business_id=business_id,
            service_id=service_id,
            resource_id=resource_id,
            requested_start=requested_start,
            tz=tz,
            exclude_appointment_id=exclude_appointment_id,
        )
        if primary:
            return True, self._offer_from(
                slots=[{"start_at": primary[0].start_at, "end_at": primary[0].end_at}],
                target_date=requested_start.astimezone(tz).date().isoformat(),
                business_id=business_id,
                conversation_id=conversation_id,
                service_id=service_id,
                service_name=service_name,
                resource_id=resource_id,
                resource_name=resource_name,
                business_timezone=business_timezone,
            )

        # If the bare time's OTHER reading is an exact bookable slot, prefer it.
        if alt_reading_start is not None:
            alt = await self._exact_match(
                business_id=business_id,
                service_id=service_id,
                resource_id=resource_id,
                requested_start=alt_reading_start,
                tz=tz,
                exclude_appointment_id=exclude_appointment_id,
            )
            if alt:
                return True, self._offer_from(
                    slots=[{"start_at": alt[0].start_at, "end_at": alt[0].end_at}],
                    target_date=alt_reading_start.astimezone(tz).date().isoformat(),
                    business_id=business_id,
                    conversation_id=conversation_id,
                    service_id=service_id,
                    service_name=service_name,
                    resource_id=resource_id,
                    resource_name=resource_name,
                    business_timezone=business_timezone,
                )

        # Neither reading is exact: gather alternatives near BOTH readings and
        # rank by proximity to whichever reading is closer.
        readings = [requested_start]
        if alt_reading_start is not None:
            readings.append(alt_reading_start)

        merged: dict[datetime, AvailableSlot] = {}
        for reading in readings:
            decision = await self._avail.check_exact_slot(
                business_id,
                service_id,
                resource_id,
                reading,
                exclude_appointment_id=exclude_appointment_id,
            )
            for slot in decision.alternatives:
                merged.setdefault(slot.start_at, slot)

        if not merged:
            return False, None

        def _min_distance(start: datetime) -> float:
            return min(abs((start - r).total_seconds()) for r in readings)

        ranked = sorted(
            merged.values(),
            key=lambda s: (_min_distance(s.start_at), s.start_at),
        )[:3]

        raw_alts: list[dict[str, object]] = [
            {"start_at": s.start_at, "end_at": s.end_at} for s in ranked
        ]
        return False, self._offer_from(
            slots=raw_alts,
            target_date=requested_start.astimezone(tz).date().isoformat(),
            business_id=business_id,
            conversation_id=conversation_id,
            service_id=service_id,
            service_name=service_name,
            resource_id=resource_id,
            resource_name=resource_name,
            business_timezone=business_timezone,
        )

    def _offer_from(
        self,
        *,
        slots: list[dict[str, object]],
        target_date: str,
        business_id: int,
        conversation_id: str,
        service_id: int,
        service_name: str,
        resource_id: int,
        resource_name: str,
        business_timezone: str,
    ) -> AvailabilityOffer:
        return build_offer(
            business_id=business_id,
            conversation_id=conversation_id,
            service_id=service_id,
            service_name=service_name,
            resource_id=resource_id,
            resource_name=resource_name,
            target_date=target_date,
            available_slots=slots,
            business_timezone=business_timezone,
        )

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
