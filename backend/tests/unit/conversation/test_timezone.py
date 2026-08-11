"""Tests for timezone-correct time construction in conversation."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fonely.domain.conversation.state import ConversationContext
from fonely.services.conversation_persistence import _deserialize_facts


class TestExtractDatetimeTimezone:
    # These exercise timezone-correct composition. A date must be present:
    # a bare time with no date no longer composes a datetime (the parser is
    # not allowed to invent today's date — see the P0 regression tests).
    def test_6pm_in_kolkata_stored_as_1230_utc(self) -> None:
        from fonely.services.conversation import ConversationService

        ctx = ConversationContext(business_id=1)
        service = ConversationService.__new__(ConversationService)
        service._extract_datetime(ctx, "tomorrow 6:00 PM appointment", timezone="Asia/Kolkata")

        assert "start_at" in ctx.collected_facts
        start_at = ctx.collected_facts["start_at"]
        assert isinstance(start_at, datetime)

        kolkata = ZoneInfo("Asia/Kolkata")
        local = start_at.astimezone(kolkata)
        assert local.hour == 18
        assert local.minute == 0

        utc = start_at.astimezone(UTC)
        assert utc.hour == 12
        assert utc.minute == 30

    def test_10am_in_kolkata_stored_as_0430_utc(self) -> None:
        from fonely.services.conversation import ConversationService

        ctx = ConversationContext(business_id=1)
        service = ConversationService.__new__(ConversationService)
        service._extract_datetime(ctx, "tomorrow 10:00 AM", timezone="Asia/Kolkata")

        start_at = ctx.collected_facts["start_at"]
        utc = start_at.astimezone(UTC)
        assert utc.hour == 4
        assert utc.minute == 30

    def test_utc_is_not_default(self) -> None:
        from fonely.services.conversation import ConversationService

        ctx = ConversationContext(business_id=1)
        service = ConversationService.__new__(ConversationService)
        service._extract_datetime(ctx, "tomorrow 6:00 PM", timezone="Asia/Kolkata")

        start_at = ctx.collected_facts["start_at"]
        assert start_at.hour != 18 or start_at.tzinfo != UTC

    def test_bare_time_no_date_does_not_compose(self) -> None:
        # The regression that motivated the change: a bare time alone must not
        # produce a start_at, because there is no date to attach it to.
        from fonely.services.conversation import ConversationService

        ctx = ConversationContext(business_id=1)
        service = ConversationService.__new__(ConversationService)
        service._extract_datetime(ctx, "6:00 PM", timezone="Asia/Kolkata")

        assert "start_at" not in ctx.collected_facts
        assert ctx.collected_facts.get("_pending_time") == "18:00:00"


class TestDeserializeFacts:
    def test_start_at_string_converted_to_datetime(self) -> None:
        facts = {"start_at": "2026-08-04T12:30:00+00:00", "service_id": 1}
        result = _deserialize_facts(facts)
        assert isinstance(result["start_at"], datetime)
        assert result["start_at"].hour == 12
        assert result["start_at"].minute == 30
        assert result["service_id"] == 1

    def test_non_datetime_keys_unchanged(self) -> None:
        facts = {"service_id": 1, "customer_name": "Karthick"}
        result = _deserialize_facts(facts)
        assert result == facts

    def test_invalid_datetime_string_kept_as_string(self) -> None:
        facts = {"start_at": "not-a-date"}
        result = _deserialize_facts(facts)
        assert result["start_at"] == "not-a-date"


class TestTurnCountRestore:
    def test_turn_count_from_db_sets_len_turns(self) -> None:
        from types import SimpleNamespace

        from fonely.services.conversation_persistence import (
            ConversationPersistenceService,
        )

        db_conv = SimpleNamespace(
            id="conv-123",
            business_id=1,
            state="fact_collection",
            collected_facts={"service_id": 1},
            proposal_id=None,
            proposal_version=None,
            created_at=datetime.now(UTC),
            turn_count=3,
        )
        ctx = ConversationPersistenceService._to_context(db_conv)
        assert ctx.turn_count == 3
        assert ctx.conversation_id == "conv-123"
        assert ctx.collected_facts["service_id"] == 1
