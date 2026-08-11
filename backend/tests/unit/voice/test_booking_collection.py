"""Tests for non-authoritative BookingCollection and receipt-gated speech.

All tests use runtime-native types only. No legacy voice-lab imports.
The conformance fake is test-only and impossible to configure in production.
"""
from __future__ import annotations

import time as time_mod
from datetime import date, time, datetime, timezone

import pytest

from fonely.voice.config import SpeechClass, VoiceSessionConfig
from fonely.voice.context import AvailableSlot, DayAvailability, TrustedClock
from fonely.voice.dialogue import BookingCollection, extract_booking_time
from fonely.voice.runtime import (
    CommandResult,
    CommitReceipt,
    PipelineRuntime,
)


def _async_avail():
    class A:
        async def query_day_availability(self, q):
            return _avail(q.target_date)
    return A()


def _avail(target_date: date) -> DayAvailability:
    return DayAvailability(
        business_date=target_date,
        day_of_week="monday",
        is_operating_day=True,
        is_exception_day=False,
        available_slots=(
            AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "scaling"),
            AvailableSlot(1, "Dr. Priya", time(17, 0), time(17, 30), "scaling"),
            AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),
        ),
    )


class TestTamilBookingIntent:
    def test_tamil_script_book_activates(self):
        bc = BookingCollection()
        bc.update("appointment புக் பண்ணனும்", resolved_date=None, availability=None)
        assert bc.active

    def test_tamil_script_book_pukkpanna(self):
        bc = BookingCollection()
        bc.update("புக் பண்ண வேணும்", resolved_date=None, availability=None)
        assert not bc.active  # needs 'appointment' prefix

    def test_full_tamil_booking(self):
        bc = BookingCollection()
        bc.update("appointment புக் பண்ணனும்", resolved_date=None, availability=None)
        assert bc.active
        assert bc.required_field == "date"

    def test_english_booking(self):
        bc = BookingCollection()
        bc.update("I want to book an appointment", resolved_date=None, availability=None)
        assert bc.active


class TestTimeExtraction:
    def test_tamil_inflected_time(self):
        assert extract_booking_time("12 மணிக்கு") == time(12, 0)

    def test_tamil_bare_time(self):
        assert extract_booking_time("12 மணி") == time(12, 0)

    def test_colon_time(self):
        assert extract_booking_time("05:00") == time(5, 0)

    def test_pm_time(self):
        assert extract_booking_time("5 pm") == time(17, 0)

    def test_am_time(self):
        assert extract_booking_time("10 am") == time(10, 0)

    def test_no_time(self):
        assert extract_booking_time("scaling வேணும்") is None


class TestStateContinuity:
    """Exact transcript: today + 12 → alternatives → select 5 PM → reason → must not re-ask date."""

    def test_exact_reported_transcript(self):
        bc = BookingCollection()
        today = date(2026, 8, 10)
        avail = _avail(today)

        bc.update(
            "இன்னைக்கு எனக்கு 12 மணிக்கு appointment புக் பண்ணனும்.",
            resolved_date=today,
            availability=avail,
        )
        assert bc.active
        assert bc.target_date == today
        assert bc.selected_time is None  # 12:00 not in offered slots

        bc.update(
            "எனக்கு 05:00 மணிக்கு ஓகே.",
            resolved_date=None,
            availability=avail,
            previous_assistant_text="12 மணிக்கு slot available இல்ல. 10, 11, 5, 6:30, 7:30 இருக்கு. எந்த time convenient?",
        )
        assert bc.target_date == today
        assert bc.selected_time == time(17, 0)  # matched 5 PM from offered
        assert bc.required_field == "reason"

        bc.update(
            "பல்லு வலிக்காக பல்லு சொத்தை. Chocolate சாப்டா.",
            resolved_date=None,
            availability=avail,
            previous_assistant_text="என்ன reason-க்காக visit பண்ணணும்?",
        )
        assert bc.target_date == today
        assert bc.selected_time == time(17, 0)
        assert bc.reason is not None
        assert bc.required_field == "name"  # NOT date or time


class TestDateChangeInvalidation:
    def test_changing_date_clears_selected_time(self):
        bc = BookingCollection()
        today = date(2026, 8, 10)
        tomorrow = date(2026, 8, 11)
        avail = _avail(today)

        bc.update("appointment புக் பண்ணனும்", resolved_date=today, availability=avail)
        bc.update("5 pm", resolved_date=None, availability=avail)
        assert bc.selected_time == time(17, 0)

        bc.update("நாளைக்கு வேணும்", resolved_date=tomorrow, availability=_avail(tomorrow))
        assert bc.target_date == tomorrow
        assert bc.selected_time is None
        assert bc.required_field == "time"

    def test_same_date_does_not_clear_time(self):
        bc = BookingCollection()
        today = date(2026, 8, 10)
        avail = _avail(today)

        bc.update("appointment புக் பண்ணனும்", resolved_date=today, availability=avail)
        bc.update("5 pm", resolved_date=None, availability=avail)
        assert bc.selected_time == time(17, 0)

        bc.update("இன்னைக்கு", resolved_date=today, availability=avail)
        assert bc.selected_time == time(17, 0)  # not cleared


class TestReceiptGatedSpeech:
    """Success speech must be impossible without a committed receipt."""

    def _clock(self):
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        local = datetime(2026, 8, 10, 14, 30, tzinfo=tz)
        return TrustedClock(
            now_utc=local.astimezone(timezone.utc),
            business_timezone="Asia/Kolkata",
            business_date=date(2026, 8, 10),
            day_of_week="monday",
        )

    def _stt(self, texts):
        class S:
            def __init__(self, t): self._t, self._i = list(t), 0
            async def transcribe(self, a):
                if self._i >= len(self._t): return ""
                t = self._t[self._i]; self._i += 1; return t
            async def close(self): pass
        return S(texts)

    def _llm(self, responses):
        class L:
            def __init__(self, r): self._r, self._i = list(r), 0
            async def generate(self, s, m):
                if self._i >= len(self._r): return ""
                r = self._r[self._i]; self._i += 1; return r
            async def close(self): pass
        return L(responses)

    def _tts(self):
        class T:
            def __init__(self): self.calls = 0
            async def synthesize(self, t): self.calls += 1; return t.encode()
            async def close(self): pass
        return T()

    @pytest.mark.asyncio
    async def test_no_command_port_blocks_success(self):
        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="no-port", business_id=1),
            clock=self._clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            stt=self._stt(["Confirm"]),
            llm=self._llm(["Booking confirmed."]),
            tts=self._tts(),
            session_mode="demo",
        )
        await rt.initialize()
        result = await rt.process_turn(b"x")
        assert not result.allowed
        assert result.commit_receipt is None
        await rt.close()

    @pytest.mark.asyncio
    async def test_error_engine_blocks_success(self):
        class ErrorEngine:
            async def propose(self, cmd):
                return CommandResult(success=False, error="application_error")
            async def confirm(self, cmd):
                return CommandResult(success=False, error="application_error")

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="err-port", business_id=1),
            clock=self._clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            stt=self._stt(["reason", "date", "time", "name", "Aamaa"]),
            llm=self._llm([
                "என்ன reason?",
                "எந்த date?",
                "Time சரியா?",
                "பேரு?",
                "Correct-ஆ?",
                "Booking confirmed.",
            ]),
            tts=self._tts(),
            availability_port=_async_avail(),
            command_port=ErrorEngine(),
            session_mode="live",
        )
        await rt.initialize()
        for _ in range(5):
            await rt.process_turn(b"x")
        result = await rt.process_turn(b"x")
        assert result.commit_receipt is None
        assert not result.allowed
        await rt.close()

    @pytest.mark.asyncio
    async def test_stale_receipt_blocked(self):
        class StaleEngine:
            async def propose(self, cmd):
                return CommandResult(success=True, operation="create", proposal_id=1)
            async def confirm(self, cmd):
                return CommandResult(
                    success=True, operation="create", proposal_id=1,
                    committed=True,
                    receipt=CommitReceipt(
                        commitment_id=1, proposal_id=1, business_id=1,
                        operation="create", idempotency_key="k",
                        confirm_idempotency_key="ck", payload_digest="",
                        committed_at_ns=0, facts={},
                    ),
                )

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="stale-port", business_id=1),
            clock=self._clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            stt=self._stt(["reason", "date", "time", "name", "Aamaa"]),
            llm=self._llm([
                "என்ன reason?", "எந்த date?", "Time?", "பேரு?",
                "Correct-ஆ?", "Booking confirmed.",
            ]),
            tts=self._tts(),
            availability_port=_async_avail(),
            command_port=StaleEngine(),
            session_mode="live",
        )
        await rt.initialize()
        for _ in range(5):
            await rt.process_turn(b"x")
        result = await rt.process_turn(b"x")
        assert not result.allowed  # committed_at_ns=0 → blocked

        await rt.close()


class _ConformanceFakeReceipt:
    """Test-only fake matching ADR 0002 committed receipt shape.

    Impossible to configure in production: requires a sentinel source
    value that no real application port produces.
    """

    _SENTINEL = "__test_conformance_only__"

    def __init__(self, *, service_name="Scaling", resource_name="Dr. Priya",
                 start_at_utc=None, end_at_utc=None, business_timezone="Asia/Kolkata",
                 notification_intent_state="queued"):
        from datetime import UTC
        now = datetime.now(UTC)
        self.status = "committed"
        self.source = self._SENTINEL
        self.operation = "create"
        self.business_id = 1
        self.appointment_id = 42
        self.proposal_id = 7
        self.proposal_version = 2
        self.confirmation_id = "conf-001"
        self.committed_at = now
        self.service_name = service_name
        self.resource_name = resource_name
        self.start_at_utc = start_at_utc or datetime(2026, 8, 11, 13, 0, tzinfo=UTC)
        self.end_at_utc = end_at_utc or datetime(2026, 8, 11, 13, 30, tzinfo=UTC)
        self.business_timezone = business_timezone
        self.customer_subject = "customer-1"
        self.payload_digest = "digest-fake"
        self.notification_intent_state = notification_intent_state
        self.offer_id = "offer-001"
        self.slot_token = "token-opaque-server-generated"


class TestConformanceFake:
    """Verify the test fake matches ADR 0002 shape without being production-usable."""

    def test_has_all_adr_fields(self):
        r = _ConformanceFakeReceipt()
        for field in [
            "status", "source", "operation", "business_id", "appointment_id",
            "proposal_id", "proposal_version", "confirmation_id", "committed_at",
            "service_name", "resource_name", "start_at_utc", "end_at_utc",
            "business_timezone", "customer_subject", "payload_digest",
            "notification_intent_state", "offer_id", "slot_token",
        ]:
            assert hasattr(r, field), f"missing field: {field}"

    def test_sentinel_prevents_production_use(self):
        r = _ConformanceFakeReceipt()
        assert r.source == "__test_conformance_only__"
        assert r.source != "appointment_service"
        assert r.source != "test_engine"

    def test_notification_state_is_valid(self):
        for state in ("queued", "not_queued", "not_applicable"):
            r = _ConformanceFakeReceipt(notification_intent_state=state)
            assert r.notification_intent_state == state

    def test_timestamps_are_aware(self):
        r = _ConformanceFakeReceipt()
        assert r.committed_at.tzinfo is not None
        assert r.start_at_utc.tzinfo is not None
        assert r.end_at_utc.tzinfo is not None
        assert r.end_at_utc > r.start_at_utc


class TestScenario2ReasonFirstThenDateSlotName:
    """Scenario 2: reason → date → offered slot → select → name → confirm."""

    def test_reason_first_then_date_preserves_all(self):
        bc = BookingCollection()
        today = date(2026, 8, 10)
        avail = _avail(today)

        bc.update("appointment புக் பண்ணனும்", resolved_date=None, availability=None)
        assert bc.active
        assert bc.required_field == "date"

        bc.update(
            "scaling வேணும்",
            resolved_date=None,
            availability=None,
            previous_assistant_text="என்ன reason?",
        )
        assert bc.reason is not None
        assert bc.required_field == "date"

        bc.update("இன்னைக்கு", resolved_date=today, availability=avail)
        assert bc.target_date == today
        assert bc.required_field == "time"

        bc.update(
            "6:30 pm",
            resolved_date=None,
            availability=avail,
            previous_assistant_text="10, 17:00, 18:30 available. எந்த time?",
        )
        assert bc.selected_time == time(18, 30)
        assert bc.required_field == "name"

        bc.update(
            "Karthick",
            resolved_date=None,
            availability=avail,
            previous_assistant_text="பேரு சொல்லுங்க?",
        )
        assert bc.patient_name == "Karthick"
        assert bc.required_field == "confirmation"


class TestScenario4TimeChangeFromFreshAvailability:
    """Scenario 4: patient changes time — select only from offered slots."""

    def test_unooffered_time_not_selected(self):
        bc = BookingCollection()
        today = date(2026, 8, 10)
        avail = _avail(today)

        bc.update("appointment புக் பண்ணனும்", resolved_date=today, availability=avail)
        bc.update("3:00 pm", resolved_date=None, availability=avail)
        assert bc.selected_time is None  # 15:00 not in offered slots

    def test_offered_time_selected(self):
        bc = BookingCollection()
        today = date(2026, 8, 10)
        avail = _avail(today)

        bc.update("appointment புக் பண்ணனும்", resolved_date=today, availability=avail)
        bc.update("10 am", resolved_date=None, availability=avail)
        assert bc.selected_time == time(10, 0)


class TestScenario5UnrelatedUtterancePreservesSlot:
    """Scenario 5: unrelated answer after selection — slot remains bound."""

    def test_reason_after_slot_preserves_selection(self):
        bc = BookingCollection()
        today = date(2026, 8, 10)
        avail = _avail(today)

        bc.update("appointment புக் பண்ணனும்", resolved_date=today, availability=avail)
        bc.update("5 pm", resolved_date=None, availability=avail)
        assert bc.selected_time == time(17, 0)

        bc.update(
            "scaling வேணும்",
            resolved_date=None,
            availability=avail,
            previous_assistant_text="என்ன reason?",
        )
        assert bc.selected_time == time(17, 0)
        assert bc.target_date == today
        assert bc.reason is not None

    def test_tangent_preserves_selection(self):
        bc = BookingCollection()
        today = date(2026, 8, 10)
        avail = _avail(today)

        bc.update("appointment புக் பண்ணனும்", resolved_date=today, availability=avail)
        bc.update("5 pm", resolved_date=None, availability=avail)

        bc.update(
            "fee எவ்வளவு?",
            resolved_date=None,
            availability=avail,
        )
        assert bc.selected_time == time(17, 0)
        assert bc.target_date == today


class TestScenario6AmbiguousTimeNoGuessing:
    """Scenario 6: ambiguous time — never guess between two matches."""

    def test_ambiguous_12h_time_not_selected(self):
        ambiguous_avail = DayAvailability(
            business_date=date(2026, 8, 10),
            day_of_week="monday",
            is_operating_day=True,
            is_exception_day=False,
            available_slots=(
                AvailableSlot(1, "Dr. Priya", time(5, 0), time(5, 30), "scaling"),
                AvailableSlot(1, "Dr. Priya", time(17, 0), time(17, 30), "scaling"),
            ),
        )
        bc = BookingCollection()
        bc.update("appointment புக் பண்ணனும்", resolved_date=date(2026, 8, 10), availability=ambiguous_avail)
        bc.update("5 o'clock", resolved_date=None, availability=ambiguous_avail)
        assert bc.selected_time is None  # ambiguous: 5 AM or 5 PM


class TestScenario7CancelNoCommit:
    """Scenario 7: explicit NO/cancel — collection resets but no authority to uncommit."""

    def test_inactive_after_no_booking_request(self):
        bc = BookingCollection()
        bc.update("fee எவ்வளவு?", resolved_date=None, availability=None)
        assert not bc.active
        assert bc.required_field is None


class TestD4NaalaikkuNotName:
    """D4: 'Naalaikku' (tomorrow) must not be captured as patient name."""

    def test_naalaikku_not_captured_as_name(self):
        bc = BookingCollection()
        today = date(2026, 8, 10)
        avail = _avail(today)

        bc.update("appointment புக் பண்ணனும்", resolved_date=today, availability=avail)
        bc.update("Scaling", resolved_date=None, availability=avail)
        bc.update(
            "Naalaikku",
            resolved_date=None,
            availability=avail,
            previous_assistant_text="உங்க பேரு சொல்லுங்க?",
        )
        assert bc.patient_name is None, f"'Naalaikku' should not be a name, got {bc.patient_name}"

    def test_tomorrow_not_captured_as_name(self):
        bc = BookingCollection()
        bc.update("appointment புக் பண்ணனும்", resolved_date=date(2026, 8, 10), availability=_avail(date(2026, 8, 10)))
        bc.update(
            "tomorrow",
            resolved_date=None,
            availability=_avail(date(2026, 8, 10)),
            previous_assistant_text="உங்க பேரு சொல்லுங்க?",
        )
        assert bc.patient_name is None

    def test_innaikku_not_captured_as_name(self):
        bc = BookingCollection()
        bc.update("appointment புக் பண்ணனும்", resolved_date=date(2026, 8, 10), availability=_avail(date(2026, 8, 10)))
        bc.update(
            "innaikku",
            resolved_date=None,
            availability=_avail(date(2026, 8, 10)),
            previous_assistant_text="பேரு சொல்லுங்க?",
        )
        assert bc.patient_name is None

    def test_real_name_still_captured(self):
        bc = BookingCollection()
        bc.update("appointment புக் பண்ணனும்", resolved_date=date(2026, 8, 10), availability=_avail(date(2026, 8, 10)))
        bc.update(
            "Karthick",
            resolved_date=None,
            availability=_avail(date(2026, 8, 10)),
            previous_assistant_text="உங்க பேரு சொல்லுங்க?",
        )
        assert bc.patient_name == "Karthick"

    def test_time_word_not_captured_as_name(self):
        bc = BookingCollection()
        bc.update("appointment புக் பண்ணனும்", resolved_date=date(2026, 8, 10), availability=_avail(date(2026, 8, 10)))
        bc.update(
            "morning",
            resolved_date=None,
            availability=_avail(date(2026, 8, 10)),
            previous_assistant_text="பேரு சொல்லுங்க?",
        )
        assert bc.patient_name is None


class TestD1MedicalSafetyEnforcement:
    """D1: treatment/medication/diagnosis in LLM output must be rejected."""

    def test_treatment_suggestion_detected(self):
        from fonely.voice.dialogue import contains_medical_advice
        assert contains_medical_advice("சொத்தைக்கு root canal தேவைப்படலாம்")
        assert contains_medical_advice("Take Paracetamol 500mg for the pain")
        assert contains_medical_advice("You need an extraction")
        assert contains_medical_advice("It could be an infection, take antibiotics")

    def test_safe_referral_not_flagged(self):
        from fonely.voice.dialogue import contains_medical_advice
        assert not contains_medical_advice("Doctor பார்த்துதான் சொல்வாங்க")
        assert not contains_medical_advice("Clinic-ஐ நேரடியாக call பண்ணுங்க")
        assert not contains_medical_advice("நாளைக்கு 10:00 slot available")
        assert not contains_medical_advice("என்ன reason-க்காக visit?")


class TestD2DeterministicReadback:
    """D2: when all fields collected, runtime must force deterministic readback."""

    def test_readback_required_when_complete(self):
        bc = BookingCollection()
        bc.active = True
        bc.reason = "scaling"
        bc.target_date = date(2026, 8, 11)
        bc.selected_time = time(18, 30)
        bc.patient_name = "Karthick"
        assert bc.required_field == "confirmation"
        readback = bc.format_readback()
        assert "Scaling" in readback
        assert "Karthick" in readback
        assert "மாலை 6:30" in readback

    def test_readback_not_generated_when_incomplete(self):
        bc = BookingCollection()
        bc.active = True
        bc.reason = "scaling"
        bc.target_date = date(2026, 8, 11)
        assert bc.required_field != "confirmation"
        assert bc.format_readback() is None


class TestD3FieldOrderEnforcement:
    """D3: runtime owns field order; LLM gets required_field, not field choice."""

    def test_required_field_order(self):
        bc = BookingCollection()
        bc.active = True
        assert bc.required_field == "date"

        bc.target_date = date(2026, 8, 11)
        assert bc.required_field == "time"

        bc.selected_time = time(18, 30)
        assert bc.required_field == "reason"

        bc.reason = "scaling"
        assert bc.required_field == "name"

        bc.patient_name = "Karthick"
        assert bc.required_field == "confirmation"

    def test_render_includes_required_field(self):
        bc = BookingCollection()
        bc.active = True
        bc.target_date = date(2026, 8, 11)
        rendered = bc.render()
        assert "required_field: time" in rendered


class TestD5NoUnpromptedAvailability:
    """D5: availability must not appear in prompt until caller states a date."""

    def test_no_availability_without_date(self):
        bc = BookingCollection()
        bc.active = True
        assert bc.target_date is None
        assert bc.should_include_availability is False

    def test_availability_after_date(self):
        bc = BookingCollection()
        bc.active = True
        bc.target_date = date(2026, 8, 11)
        assert bc.should_include_availability is True


class TestReceiptKeyedGate:
    """Gate keys on receipt existence, not conversation state."""

    def test_success_blocked_without_receipt(self):
        from fonely.voice.dialogue import gate_response
        text, suppressed = gate_response(
            "உங்கள் appointment confirm ஆயிடுச்சு!", has_receipt=False,
        )
        assert suppressed
        assert "confirm" not in text.lower()
        assert "verify" in text or "காத்திருங்க" in text

    def test_success_allowed_with_receipt(self):
        from fonely.voice.dialogue import gate_response
        text, suppressed = gate_response(
            "உங்கள் appointment confirm ஆயிடுச்சு!", has_receipt=True,
        )
        assert not suppressed
        assert "confirm" in text.lower()

    def test_non_success_passes_without_receipt(self):
        from fonely.voice.dialogue import gate_response
        text, suppressed = gate_response(
            "எந்த date-ல வரணும்?", has_receipt=False,
        )
        assert not suppressed
        assert text == "எந்த date-ல வரணும்?"

    def test_medical_blocked_regardless_of_receipt(self):
        from fonely.voice.dialogue import gate_response
        text, suppressed = gate_response(
            "Take Paracetamol 500mg", has_receipt=True,
        )
        assert suppressed
        assert "doctor" in text.lower() or "Doctor" in text

    def test_recovery_text_is_coherent_tamil(self):
        from fonely.voice.dialogue import gate_response, SAFE_NO_RECEIPT
        text, _ = gate_response("Booking confirmed!", has_receipt=False)
        assert text == SAFE_NO_RECEIPT
        assert any("஀" <= c <= "௿" for c in text)
        assert len(text) > 10

    def test_preconfirmation_success_blocked(self):
        """The exact defect: model speaks success before caller confirms."""
        from fonely.voice.dialogue import gate_response
        text, suppressed = gate_response(
            "உங்கள் appointment confirm பண்ணிட்டோம், Karthick.",
            has_receipt=False,
        )
        assert suppressed

    def test_tanglish_success_blocked(self):
        from fonely.voice.dialogue import gate_response
        text, suppressed = gate_response(
            "Booking fix aayiduchu bro!", has_receipt=False,
        )
        assert suppressed


class TestBookingCollectionRender:
    def test_render_includes_all_fields(self):
        bc = BookingCollection()
        bc.active = True
        bc.target_date = date(2026, 8, 10)
        bc.selected_time = time(17, 0)
        bc.reason = "scaling"
        rendered = bc.render()
        assert "active: true" in rendered
        assert "target_date: 2026-08-10" in rendered
        assert "selected_time: 17:00" in rendered
        assert "reason: scaling" in rendered
        assert "required_field: name" in rendered
        assert "cannot authorize" in rendered
