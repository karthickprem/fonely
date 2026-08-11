"""Unit tests for the booking application contract types."""

from datetime import UTC, datetime, timedelta

from fonely.domain.booking.contract import (
    AvailabilityOffer,
    AvailabilitySlot,
    SelectedSlot,
)

NOW = datetime(2026, 8, 15, 4, 30, tzinfo=UTC)
NOW_END = NOW + timedelta(minutes=30)
EXPIRES = NOW + timedelta(minutes=15)


class TestAvailabilityOffer:
    def test_generate_token_deterministic(self) -> None:
        t1 = AvailabilityOffer.generate_token("offer-1", NOW, NOW_END, 1, 1, EXPIRES)
        t2 = AvailabilityOffer.generate_token("offer-1", NOW, NOW_END, 1, 1, EXPIRES)
        assert t1 == t2
        assert len(t1) == 16

    def test_generate_token_varies_by_offer(self) -> None:
        t1 = AvailabilityOffer.generate_token("offer-1", NOW, NOW_END, 1, 1, EXPIRES)
        t2 = AvailabilityOffer.generate_token("offer-2", NOW, NOW_END, 1, 1, EXPIRES)
        assert t1 != t2

    def test_generate_token_varies_by_service(self) -> None:
        t1 = AvailabilityOffer.generate_token("offer-1", NOW, NOW_END, 1, 1, EXPIRES)
        t2 = AvailabilityOffer.generate_token("offer-1", NOW, NOW_END, 1, 99, EXPIRES)
        assert t1 != t2

    def test_generate_token_varies_by_expires(self) -> None:
        t1 = AvailabilityOffer.generate_token("offer-1", NOW, NOW_END, 1, 1, EXPIRES)
        t2 = AvailabilityOffer.generate_token(
            "offer-1", NOW, NOW_END, 1, 1, EXPIRES + timedelta(days=30)
        )
        assert t1 != t2

    def test_find_by_token(self) -> None:
        slot = AvailabilitySlot(
            token="abc123",
            start_at_utc=NOW,
            end_at_utc=NOW_END,
            display_date="Friday, Aug 15",
            display_time="10:00 AM",
            display_end_time="10:30 AM",
        )
        offer = AvailabilityOffer(
            offer_id="test",
            revision=1,
            business_id=1,
            conversation_id="conv-1",
            service_id=1,
            service_name="Consultation",
            resource_id=1,
            resource_name="Dr. Priya",
            target_date="2026-08-15",
            slots=(slot,),
            created_at=NOW,
            expires_at=EXPIRES,
        )
        assert offer.find_by_token("abc123") is slot
        assert offer.find_by_token("nonexistent") is None

    def test_is_expired(self) -> None:
        offer = AvailabilityOffer(
            offer_id="test",
            revision=1,
            business_id=1,
            conversation_id="conv-1",
            service_id=1,
            service_name="C",
            resource_id=1,
            resource_name="D",
            target_date="2026-08-15",
            slots=(),
            created_at=NOW,
            expires_at=EXPIRES,
        )
        assert not offer.is_expired(NOW)
        assert offer.is_expired(NOW + timedelta(minutes=16))

    def test_new_offer_id_unique(self) -> None:
        ids = {AvailabilityOffer.new_offer_id() for _ in range(100)}
        assert len(ids) == 100


class TestSelectedSlot:
    def test_immutable(self) -> None:
        s = SelectedSlot(
            offer_id="o1",
            offer_revision=1,
            token="t1",
            start_at_utc=NOW,
            end_at_utc=NOW_END,
            service_id=1,
            resource_id=1,
        )
        assert s.offer_id == "o1"
        assert s.token == "t1"
