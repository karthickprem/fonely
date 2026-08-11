"""Durable offered-slot management.

Availability offers are persisted in the conversation's collected_facts
JSON under the `_active_offer` key. No migration required.

Validates token membership, expiry, revision, and cross-conversation/tenant
binding. Fail-closed on malformed or orphan state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fonely.domain.booking.contract import (
    AvailabilityOffer,
    AvailabilitySlot,
    SelectedSlot,
)

OFFER_TTL_MINUTES = 15


class OfferValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def build_offer(
    *,
    business_id: int,
    conversation_id: str,
    service_id: int,
    service_name: str,
    resource_id: int,
    resource_name: str,
    target_date: str,
    available_slots: list[dict[str, object]],
    business_timezone: str,
) -> AvailabilityOffer:
    now = datetime.now(UTC)
    offer_id = AvailabilityOffer.new_offer_id()
    tz = ZoneInfo(business_timezone)

    if len(available_slots) > 100:
        raise OfferValidationError("too_many_slots", "Maximum 100 slots per offer")

    slots: list[AvailabilitySlot] = []
    for raw in available_slots:
        start = raw["start_at"]
        end = raw["end_at"]
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise OfferValidationError("invalid_slot_datetime", "Slot start/end must be datetime")
        if start.tzinfo is None or end.tzinfo is None:
            raise OfferValidationError("naive_datetime", "Slot datetimes must be timezone-aware")
        if end <= start:
            raise OfferValidationError("invalid_slot_interval", "Slot end must be after start")
        local_start = start.astimezone(tz)
        local_end = end.astimezone(tz)
        expires_at = now + timedelta(minutes=OFFER_TTL_MINUTES)
        token = AvailabilityOffer.generate_token(
            offer_id, start, end, resource_id, service_id, expires_at
        )
        slots.append(
            AvailabilitySlot(
                token=token,
                start_at_utc=start,
                end_at_utc=end,
                display_date=local_start.strftime("%A, %b %d"),
                display_time=local_start.strftime("%-I:%M %p"),
                display_end_time=local_end.strftime("%-I:%M %p"),
            )
        )

    return AvailabilityOffer(
        offer_id=offer_id,
        revision=1,
        business_id=business_id,
        conversation_id=conversation_id,
        service_id=service_id,
        service_name=service_name,
        resource_id=resource_id,
        resource_name=resource_name,
        target_date=target_date,
        slots=tuple(slots),
        created_at=now,
        expires_at=expires_at,
    )


def validate_selection(
    offer: AvailabilityOffer,
    token: str,
    *,
    business_id: int,
    conversation_id: str,
) -> SelectedSlot:
    now = datetime.now(UTC)

    if offer.business_id != business_id:
        raise OfferValidationError("cross_tenant", "Offer does not belong to this business")
    if offer.conversation_id != conversation_id:
        raise OfferValidationError(
            "cross_conversation", "Offer does not belong to this conversation"
        )
    if offer.is_expired(now):
        raise OfferValidationError("expired", "Offer has expired")

    slot = offer.find_by_token(token)
    if slot is None:
        raise OfferValidationError("invalid_token", "Token does not match any offered slot")

    return SelectedSlot(
        offer_id=offer.offer_id,
        offer_revision=offer.revision,
        token=token,
        start_at_utc=slot.start_at_utc,
        end_at_utc=slot.end_at_utc,
        service_id=offer.service_id,
        resource_id=offer.resource_id,
    )


def serialize_offer(offer: AvailabilityOffer) -> dict[str, object]:
    return {
        "offer_id": offer.offer_id,
        "revision": offer.revision,
        "business_id": offer.business_id,
        "conversation_id": offer.conversation_id,
        "service_id": offer.service_id,
        "service_name": offer.service_name,
        "resource_id": offer.resource_id,
        "resource_name": offer.resource_name,
        "target_date": offer.target_date,
        "slots": [
            {
                "token": s.token,
                "start_at_utc": s.start_at_utc.isoformat(),
                "end_at_utc": s.end_at_utc.isoformat(),
                "display_date": s.display_date,
                "display_time": s.display_time,
                "display_end_time": s.display_end_time,
            }
            for s in offer.slots
        ],
        "created_at": offer.created_at.isoformat(),
        "expires_at": offer.expires_at.isoformat(),
    }


def _parse_aware_dt(raw: object, field: str) -> datetime:
    dt = datetime.fromisoformat(str(raw))
    if dt.tzinfo is None:
        raise OfferValidationError("naive_datetime", f"{field} must be timezone-aware")
    return dt


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError("expected int-coercible value")
    return int(value)


def _as_str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected string value")
    return value


def _slot_field(slot: object, key: str) -> object:
    if not isinstance(slot, dict):
        raise ValueError("slot is not a mapping")
    return slot[key]


def deserialize_offer(data: dict[str, object]) -> AvailabilityOffer | None:
    if not data:
        return None
    try:
        offer_id = _as_str(data["offer_id"])
        resource_id = _as_int(data["resource_id"])
        service_id = _as_int(data["service_id"])
        created_at = _parse_aware_dt(data["created_at"], "created_at")
        expires_at = _parse_aware_dt(data["expires_at"], "expires_at")

        if expires_at <= created_at:
            raise OfferValidationError("invalid_expiry", "expires_at must be after created_at")

        ttl = (expires_at - created_at).total_seconds() / 60
        if ttl > OFFER_TTL_MINUTES + 1:
            raise OfferValidationError("ttl_exceeded", "Offer TTL exceeds maximum")

        raw_slots = data.get("slots", [])
        if not isinstance(raw_slots, list) or len(raw_slots) > 100:
            raise OfferValidationError("invalid_slots", "Slots malformed or too many")

        slots: list[AvailabilitySlot] = []
        for s in raw_slots:
            start = _parse_aware_dt(_slot_field(s, "start_at_utc"), "slot.start_at_utc")
            end = _parse_aware_dt(_slot_field(s, "end_at_utc"), "slot.end_at_utc")
            if end <= start:
                raise OfferValidationError("invalid_slot_interval", "Slot end must be after start")
            expected_token = AvailabilityOffer.generate_token(
                offer_id, start, end, resource_id, service_id, expires_at
            )
            stored_token = _as_str(_slot_field(s, "token"))
            if stored_token != expected_token:
                raise OfferValidationError(
                    "tampered_token",
                    "Stored token does not match recomputed token",
                )
            slots.append(
                AvailabilitySlot(
                    token=stored_token,
                    start_at_utc=start,
                    end_at_utc=end,
                    display_date=_as_str(_slot_field(s, "display_date")),
                    display_time=_as_str(_slot_field(s, "display_time")),
                    display_end_time=_as_str(_slot_field(s, "display_end_time")),
                )
            )

        return AvailabilityOffer(
            offer_id=offer_id,
            revision=_as_int(data["revision"]),
            business_id=_as_int(data["business_id"]),
            conversation_id=_as_str(data["conversation_id"]),
            service_id=service_id,
            service_name=_as_str(data["service_name"]),
            resource_id=resource_id,
            resource_name=_as_str(data["resource_name"]),
            target_date=_as_str(data["target_date"]),
            slots=tuple(slots),
            created_at=created_at,
            expires_at=expires_at,
        )
    except OfferValidationError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        import logging

        logging.getLogger("fonely.domain.booking.offers").warning(
            "offer_deserialization_failed: %s", exc
        )
        return None
