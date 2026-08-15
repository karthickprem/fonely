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


def _now() -> datetime:
    """Run-relative anchor, resolved per call.

    validate_selection() compares an offer's expires_at against
    datetime.now(UTC) at RUN time. A fixed absolute NOW (this was
    datetime(2026, 8, 15, 4, 30)) turns every offer built at NOW + a few minutes
    into a time bomb: once wall-clock passes that expiry the offer is expired and
    validate_selection raises 'expired' before the assertion under test — which
    is exactly how test_valid_selection / test_invalid_token_rejected went red on
    2026-08-15. Anchoring to now() per call makes the offer's expiry always in
    the future relative to run time, so the tests cannot expire mid-suite no
    matter when they run. Tests that WANT an expired offer construct an
    explicitly past expires_at (see test_expired_rejected).
    """
    return datetime.now(UTC)


# Target date is only a display/opaque string in these offers (build_offer and
# validate_selection do not parse it against "today"), so a fixed value is safe
# and keeps the display assertions stable.
TARGET_DATE = "2026-08-15"


def _raw_slots() -> list[dict[str, object]]:
    now = _now()
    return [
        {
            "start_at": now,
            "end_at": now + timedelta(minutes=30),
        },
        {
            "start_at": now + timedelta(minutes=30),
            "end_at": now + timedelta(hours=1),
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
        now = _now()
        slot = AvailabilitySlot(
            token="valid-token",
            start_at_utc=now,
            end_at_utc=now + timedelta(minutes=30),
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
            "target_date": TARGET_DATE,
            "slots": (slot,),
            "created_at": now,
            # Comfortably in the future relative to RUN time (not a fixed clock
            # value) so validate_selection's now()-comparison cannot see it as
            # expired mid-suite. Tests that want expiry override this explicitly.
            "expires_at": now + timedelta(hours=1),
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
        now = _now()
        with pytest.raises(OfferValidationError) as exc:
            build_offer(
                business_id=1,
                conversation_id="c",
                service_id=1,
                service_name="C",
                resource_id=1,
                resource_name="D",
                target_date=TARGET_DATE,
                available_slots=[{"start_at": now + timedelta(hours=1), "end_at": now}],
                business_timezone="Asia/Kolkata",
            )
        assert exc.value.code == "invalid_slot_interval"
