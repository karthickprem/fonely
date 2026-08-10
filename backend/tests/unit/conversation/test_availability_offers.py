"""Focused unit evidence for persisted availability offers."""

from datetime import UTC, date, datetime, timedelta

import pytest

from fonely.services.availability import AvailableSlot
from fonely.services.availability_offers import (
    AvailabilityOffer,
    OfferSelectionStatus,
    create_availability_offer,
    select_from_offer,
)

_NOW = datetime(2026, 8, 10, 4, 0, tzinfo=UTC)


def _slot(hour: int, *, resource_id: int = 7, resource_name: str = "Dr. Priya") -> AvailableSlot:
    start = datetime(2026, 8, 10, hour, 0, tzinfo=UTC)
    return AvailableSlot(
        start_at=start,
        end_at=start + timedelta(minutes=30),
        resource_id=resource_id,
        resource_name=resource_name,
    )


def _offer(*slots: AvailableSlot) -> AvailabilityOffer:
    return create_availability_offer(
        business_id=1,
        conversation_id="conv-offer",
        service_id=3,
        business_timezone="Asia/Kolkata",
        alternatives=tuple(slots),
        now=_NOW,
    )


def test_offer_serialization_round_trip_preserves_revision() -> None:
    offer = _offer(_slot(5), _slot(6))

    restored = AvailabilityOffer.deserialize(offer.serialize())

    assert restored == offer
    assert restored.target_date == date(2026, 8, 10)
    assert len({slot.slot_token for slot in restored.slots}) == 2


def test_unknown_and_tampered_token_are_rejected() -> None:
    offer = _offer(_slot(5))
    token = offer.slots[0].slot_token

    assert select_from_offer(offer, token, now=_NOW).status == OfferSelectionStatus.SELECTED
    assert (
        select_from_offer(offer, token[:-1] + "x", now=_NOW).status == OfferSelectionStatus.NO_MATCH
    )


def test_offer_revision_detects_persisted_fact_tampering() -> None:
    offer = _offer(_slot(5))
    value = offer.serialize()
    value["slots"][0]["resource_id"] = 99

    with pytest.raises(ValueError, match="invalid"):
        AvailabilityOffer.deserialize(value)


def test_offer_expiry_is_inclusive() -> None:
    offer = _offer(_slot(5))

    assert (
        select_from_offer(offer, "1", now=offer.expires_at).status == OfferSelectionStatus.EXPIRED
    )


def test_duplicate_wall_time_requires_ordinal_selection() -> None:
    offer = _offer(
        _slot(5, resource_id=7, resource_name="Dr. Priya"),
        _slot(5, resource_id=8, resource_name="Dr. Arjun"),
    )

    assert select_from_offer(offer, "10:30 AM", now=_NOW).status == OfferSelectionStatus.AMBIGUOUS
    selection = select_from_offer(offer, "the second one", now=_NOW)
    assert selection.status == OfferSelectionStatus.SELECTED
    assert selection.slot is not None
    assert selection.slot.resource_id == 8


@pytest.mark.parametrize(
    ("utterance", "status"),
    [
        ("5 PM doesn't work; anything else?", OfferSelectionStatus.REJECTED),
        ("not 5 PM, make it 6 PM", OfferSelectionStatus.SCOPE_CHANGE),
        ("August 11 at 11 AM", OfferSelectionStatus.SCOPE_CHANGE),
        ("option 2 at 5 PM", OfferSelectionStatus.SCOPE_CHANGE),
    ],
)
def test_whole_utterance_does_not_select_first_time(
    utterance: str, status: OfferSelectionStatus
) -> None:
    offer = _offer(_slot(5), _slot(6))

    selection = select_from_offer(offer, utterance, now=_NOW)

    assert selection.status == status
    assert selection.slot is None


def test_offer_rejects_wrong_parent_identity() -> None:
    offer = _offer(_slot(5))
    value = offer.serialize()
    value["business_id"] = 2

    with pytest.raises(ValueError, match="invalid"):
        AvailabilityOffer.deserialize(value)
