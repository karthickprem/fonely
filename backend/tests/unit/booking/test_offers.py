"""Unit tests for durable offered-slot management."""

from datetime import UTC, datetime, timedelta

import pytest

from fonely.domain.booking.contract import AvailabilityOffer, AvailabilitySlot
from fonely.domain.booking.offers import (
    OfferValidationError,
    build_offer,
    deserialize_offer,
    serialize_offer,
    validate_selection,
)

NOW = datetime(2026, 8, 15, 4, 30, tzinfo=UTC)


def _raw_slots() -> list[dict[str, object]]:
    return [
        {
            "start_at": NOW,
            "end_at": NOW + timedelta(minutes=30),
        },
        {
            "start_at": NOW + timedelta(minutes=30),
            "end_at": NOW + timedelta(hours=1),
        },
    ]


class TestBuildOffer:
    def test_produces_tokens(self) -> None:
        offer = build_offer(
            business_id=1,
            conversation_id="conv-1",
            service_id=1,
            service_name="Consultation",
            resource_id=1,
            resource_name="Dr. Priya",
            target_date="2026-08-15",
            available_slots=_raw_slots(),
            business_timezone="Asia/Kolkata",
        )
        assert len(offer.slots) == 2
        assert offer.slots[0].token != offer.slots[1].token
        assert len(offer.slots[0].token) == 16

    def test_display_in_local_timezone(self) -> None:
        offer = build_offer(
            business_id=1,
            conversation_id="conv-1",
            service_id=1,
            service_name="C",
            resource_id=1,
            resource_name="D",
            target_date="2026-08-15",
            available_slots=_raw_slots(),
            business_timezone="Asia/Kolkata",
        )
        assert "AM" in offer.slots[0].display_time or "PM" in offer.slots[0].display_time

    def test_binds_business_and_conversation(self) -> None:
        offer = build_offer(
            business_id=42,
            conversation_id="conv-99",
            service_id=1,
            service_name="C",
            resource_id=1,
            resource_name="D",
            target_date="2026-08-15",
            available_slots=_raw_slots(),
            business_timezone="Asia/Kolkata",
        )
        assert offer.business_id == 42
        assert offer.conversation_id == "conv-99"


class TestValidateSelection:
    def _offer(self, **overrides: object) -> AvailabilityOffer:
        slot = AvailabilitySlot(
            token="valid-token",
            start_at_utc=NOW,
            end_at_utc=NOW + timedelta(minutes=30),
            display_date="Friday",
            display_time="10:00 AM",
            display_end_time="10:30 AM",
        )
        defaults = {
            "offer_id": "test",
            "revision": 1,
            "business_id": 1,
            "conversation_id": "conv-1",
            "service_id": 1,
            "service_name": "C",
            "resource_id": 1,
            "resource_name": "D",
            "target_date": "2026-08-15",
            "slots": (slot,),
            "created_at": NOW,
            "expires_at": NOW + timedelta(minutes=15),
        }
        defaults.update(overrides)
        return AvailabilityOffer(**defaults)

    def test_valid_selection(self) -> None:
        offer = self._offer()
        selected = validate_selection(offer, "valid-token", business_id=1, conversation_id="conv-1")
        assert selected.token == "valid-token"
        assert selected.offer_id == "test"
        assert selected.service_id == 1

    def test_cross_tenant_rejected(self) -> None:
        offer = self._offer(business_id=1)
        with pytest.raises(OfferValidationError) as exc:
            validate_selection(offer, "valid-token", business_id=2, conversation_id="conv-1")
        assert exc.value.code == "cross_tenant"

    def test_cross_conversation_rejected(self) -> None:
        offer = self._offer(conversation_id="conv-1")
        with pytest.raises(OfferValidationError) as exc:
            validate_selection(offer, "valid-token", business_id=1, conversation_id="conv-2")
        assert exc.value.code == "cross_conversation"

    def test_expired_rejected(self) -> None:
        past = datetime(2020, 1, 1, tzinfo=UTC)
        offer = self._offer(expires_at=past)
        with pytest.raises(OfferValidationError) as exc:
            validate_selection(offer, "valid-token", business_id=1, conversation_id="conv-1")
        assert exc.value.code == "expired"

    def test_invalid_token_rejected(self) -> None:
        offer = self._offer()
        with pytest.raises(OfferValidationError) as exc:
            validate_selection(offer, "wrong-token", business_id=1, conversation_id="conv-1")
        assert exc.value.code == "invalid_token"


class TestSerialization:
    def test_roundtrip(self) -> None:
        offer = build_offer(
            business_id=1,
            conversation_id="conv-1",
            service_id=1,
            service_name="Consultation",
            resource_id=1,
            resource_name="Dr. Priya",
            target_date="2026-08-15",
            available_slots=_raw_slots(),
            business_timezone="Asia/Kolkata",
        )
        data = serialize_offer(offer)
        restored = deserialize_offer(data)
        assert restored is not None
        assert restored.offer_id == offer.offer_id
        assert len(restored.slots) == len(offer.slots)
        assert restored.slots[0].token == offer.slots[0].token
        assert restored.business_id == offer.business_id

    def test_deserialize_empty_returns_none(self) -> None:
        assert deserialize_offer({}) is None

    def test_deserialize_malformed_returns_none(self) -> None:
        assert deserialize_offer({"offer_id": "x"}) is None

    def test_tampered_token_rejected(self) -> None:
        offer = build_offer(
            business_id=1,
            conversation_id="conv-1",
            service_id=1,
            service_name="C",
            resource_id=1,
            resource_name="D",
            target_date="2026-08-15",
            available_slots=_raw_slots(),
            business_timezone="Asia/Kolkata",
        )
        data = serialize_offer(offer)
        data["slots"][0]["token"] = "tampered"  # type: ignore[index]
        with pytest.raises(OfferValidationError) as exc:
            deserialize_offer(data)
        assert exc.value.code == "tampered_token"

    def test_naive_datetime_rejected(self) -> None:
        offer = build_offer(
            business_id=1,
            conversation_id="conv-1",
            service_id=1,
            service_name="C",
            resource_id=1,
            resource_name="D",
            target_date="2026-08-15",
            available_slots=_raw_slots(),
            business_timezone="Asia/Kolkata",
        )
        data = serialize_offer(offer)
        data["slots"][0]["start_at_utc"] = "2026-08-15T04:30:00"  # type: ignore[index]
        with pytest.raises(OfferValidationError) as exc:
            deserialize_offer(data)
        assert exc.value.code == "naive_datetime"

    def test_invalid_expiry_rejected(self) -> None:
        offer = build_offer(
            business_id=1,
            conversation_id="conv-1",
            service_id=1,
            service_name="C",
            resource_id=1,
            resource_name="D",
            target_date="2026-08-15",
            available_slots=_raw_slots(),
            business_timezone="Asia/Kolkata",
        )
        data = serialize_offer(offer)
        data["expires_at"] = data["created_at"]
        with pytest.raises(OfferValidationError) as exc:
            deserialize_offer(data)
        assert exc.value.code == "invalid_expiry"

    def test_invalid_slot_interval_rejected(self) -> None:
        offer = build_offer(
            business_id=1,
            conversation_id="conv-1",
            service_id=1,
            service_name="C",
            resource_id=1,
            resource_name="D",
            target_date="2026-08-15",
            available_slots=_raw_slots(),
            business_timezone="Asia/Kolkata",
        )
        data = serialize_offer(offer)
        data["slots"][0]["end_at_utc"] = data["slots"][0]["start_at_utc"]  # type: ignore[index]
        with pytest.raises(OfferValidationError) as exc:
            deserialize_offer(data)
        assert exc.value.code == "invalid_slot_interval"


class TestBuildOfferValidation:
    def test_naive_slot_rejected(self) -> None:
        from datetime import datetime as dt

        naive = dt(2026, 8, 15, 10, 0)
        with pytest.raises(OfferValidationError) as exc:
            build_offer(
                business_id=1,
                conversation_id="c",
                service_id=1,
                service_name="C",
                resource_id=1,
                resource_name="D",
                target_date="2026-08-15",
                available_slots=[{"start_at": naive, "end_at": naive}],
                business_timezone="Asia/Kolkata",
            )
        assert exc.value.code == "naive_datetime"

    def test_end_before_start_rejected(self) -> None:
        with pytest.raises(OfferValidationError) as exc:
            build_offer(
                business_id=1,
                conversation_id="c",
                service_id=1,
                service_name="C",
                resource_id=1,
                resource_name="D",
                target_date="2026-08-15",
                available_slots=[{"start_at": NOW + timedelta(hours=1), "end_at": NOW}],
                business_timezone="Asia/Kolkata",
            )
        assert exc.value.code == "invalid_slot_interval"
