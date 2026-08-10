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
