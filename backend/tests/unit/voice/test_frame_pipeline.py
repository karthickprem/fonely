"""Unit tests for the streaming FrameProcessors.

Covers the deterministic gate order, the single-commit path wiring, and the
precedence-bug case the lab version got wrong (`A and B or C or D` pinged the
doctor on almost any input).
"""

from __future__ import annotations

from datetime import date, time

import pytest
from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from fonely.voice.context import TrustedClock
from fonely.voice.frame_pipeline import (
    BookingPostLLMGate,
    BookingStateInjector,
    ResolverContext,
    _is_confirmation,
)
from fonely.voice.language import DEFAULT_LANGUAGE, get_response

CLOCK = TrustedClock(
    now_utc=None,
    business_timezone="Asia/Kolkata",
    business_date=date(2026, 8, 12),
    day_of_week="wednesday",
)


def _resolver(ask_doctor=None):
    return ResolverContext(
        business_id=1,
        session_factory=None,
        command_port=None,
        clock=CLOCK,
        ask_doctor=ask_doctor,
    )


class _Collector:
    """Captures frames a processor pushes downstream."""

    def __init__(self):
        self.frames = []

    async def _cb(self, frame, direction):
        self.frames.append(frame)

    def texts(self):
        return [f.text for f in self.frames if isinstance(f, LLMTextFrame)]


async def _drive_gate(gate, text):
    """Feed a complete LLM response (start, text, end) through the gate."""
    collector = _Collector()
    gate.push_frame = collector._cb
    await gate.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(LLMTextFrame(text=text), FrameDirection.DOWNSTREAM)
    await gate.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    return collector


class TestConfirmationDetection:
    def test_plain_confirmations(self):
        for w in ("yes", "ஆமா", "correct", "sari", "ok"):
            assert _is_confirmation(w)

    def test_confirmation_with_punctuation(self):
        assert _is_confirmation("yes.")
        assert _is_confirmation("சரி!")

    def test_non_confirmation(self):
        assert not _is_confirmation("no")
        assert not _is_confirmation("naalaikku")

    def test_multi_word_pure_agreement(self):
        # Regression: "ஆமா சரி" / "yes correct" are natural confirmations but
        # exact-match missed them, so the booking never confirmed or committed.
        assert _is_confirmation("ஆமா சரி")
        assert _is_confirmation("yes correct")
        assert _is_confirmation("ok sure")
        assert _is_confirmation("சரி சரி")

    def test_stt_confirmation_forms(self):
        # Real Sarvam STT renders a spoken "ஆமா சரி" as the fuller "ஆமாம், சரி".
        # The short forms alone left a spoken yes unrecognised and the booking
        # never committed through real audio. Found by the STT-on-audio proof.
        assert _is_confirmation("ஆமாம்")
        assert _is_confirmation("ஆமாம், சரி.")
        assert _is_confirmation("ஆமாம் சரி")

    def test_agreement_with_other_content_is_not_confirmation(self):
        # "yes but change the time" contains a confirm word but carries other
        # content — must NOT confirm, or a correction would book prematurely.
        assert not _is_confirmation("yes but change the time to 6")
        assert not _is_confirmation("ஆமா ஆனா நேரம் மாத்துங்க")


class TestMedicalGate:
    @pytest.mark.asyncio
    async def test_medical_advice_replaced(self):
        injector = BookingStateInjector(_resolver())
        gate = BookingPostLLMGate(injector, _resolver())
        collector = await _drive_gate(gate, "take paracetamol twice daily")
        assert get_response("medical_safe", DEFAULT_LANGUAGE) in collector.texts()

    @pytest.mark.asyncio
    async def test_normal_response_passes(self):
        injector = BookingStateInjector(_resolver())
        gate = BookingPostLLMGate(injector, _resolver())
        collector = await _drive_gate(gate, "எந்த நேரம் வேணும்?")
        assert "எந்த நேரம் வேணும்?" in collector.texts()


class TestClosureGate:
    @pytest.mark.asyncio
    async def test_goodbye_after_close(self):
        injector = BookingStateInjector(_resolver())
        injector.caller_confirmed = True
        injector.booking_closed = True
        gate = BookingPostLLMGate(injector, _resolver())
        collector = await _drive_gate(gate, "anything the model said")
        assert get_response("goodbye", DEFAULT_LANGUAGE) in collector.texts()


class TestReadbackGate:
    @pytest.mark.asyncio
    async def test_forced_readback_when_model_skips(self):
        injector = BookingStateInjector(_resolver())
        # Manually complete the booking state to reach confirmation.
        bc = injector.booking
        bc.active = True
        bc.reason = "scaling"
        bc.target_date = date(2026, 8, 12)
        bc.selected_time = time(17, 0)
        bc.patient_name = "Karthick"
        assert bc.required_field == "confirmation"

        gate = BookingPostLLMGate(injector, _resolver())
        # Model said something WITHOUT the readback confirmation phrase.
        collector = await _drive_gate(gate, "Great, all set!")
        texts = collector.texts()
        # The deterministic readback (spoken format) is forced instead.
        assert any("Karthick" in t and "correct" in t.lower() for t in texts)


class TestPrecedenceBugCase:
    """The lab version's condition was:
        "No confirmed availability" in ctx and "availability" in u
        or "slot" in u or "available" in u or "time" in u or ...
    Python binds this as (A and B) or C or D..., so ANY of C/D/... alone
    pinged the doctor even when availability WAS confirmed. The fixed version
    only pings when BOTH the schedule is unconfirmed AND the caller is asking.
    This test pins that behavior.
    """

    @pytest.mark.asyncio
    async def test_no_doctor_ping_when_availability_confirmed(self):
        pinged = []

        async def fake_ask(question, ctx):
            pinged.append((question, ctx))

        # A session_factory whose clinic_context reports CONFIRMED slots.
        import fonely.voice.clinic_resolver as cr

        orig = cr.clinic_context_text

        async def fake_ctx(session, business_id):
            return "Clinic: Test.\nAvailable slots for Wednesday: Dr. X: 17:00"

        cr.clinic_context_text = fake_ctx
        try:

            class _NullSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

            injector = BookingStateInjector(
                ResolverContext(
                    business_id=1,
                    session_factory=lambda: _NullSession(),
                    command_port=None,
                    clock=CLOCK,
                    ask_doctor=fake_ask,
                )
            )
            # Caller message contains "time" — one of the words that alone
            # triggered the buggy ping. With slots confirmed, must NOT ping.
            ctx = await injector._build_live_context("what time works")
            assert pinged == [], (
                "Doctor was pinged even though availability was confirmed — "
                "the precedence bug is back."
            )
            assert "17:00" in ctx
        finally:
            cr.clinic_context_text = orig

    @pytest.mark.asyncio
    async def test_doctor_pinged_when_unconfirmed_and_asking(self):
        pinged = []

        async def fake_ask(question, ctx):
            pinged.append((question, ctx))

        import fonely.voice.clinic_resolver as cr

        orig = cr.clinic_context_text

        async def fake_ctx(session, business_id):
            return "Wednesday: No confirmed availability. The doctor has not confirmed."

        cr.clinic_context_text = fake_ctx
        try:

            class _NullSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

            injector = BookingStateInjector(
                ResolverContext(
                    business_id=1,
                    session_factory=lambda: _NullSession(),
                    command_port=None,
                    clock=CLOCK,
                    ask_doctor=fake_ask,
                )
            )
            await injector._build_live_context("do you have any slots")
            assert len(pinged) == 1
        finally:
            cr.clinic_context_text = orig

    @pytest.mark.asyncio
    async def test_no_ping_when_unconfirmed_but_not_asking(self):
        """Unconfirmed schedule but the caller said something unrelated —
        no ping. Both conditions are required."""
        pinged = []

        async def fake_ask(question, ctx):
            pinged.append(1)

        import fonely.voice.clinic_resolver as cr

        orig = cr.clinic_context_text

        async def fake_ctx(session, business_id):
            return "Wednesday: No confirmed availability."

        cr.clinic_context_text = fake_ctx
        try:

            class _NullSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

            injector = BookingStateInjector(
                ResolverContext(
                    business_id=1,
                    session_factory=lambda: _NullSession(),
                    command_port=None,
                    clock=CLOCK,
                    ask_doctor=fake_ask,
                )
            )
            await injector._build_live_context("my name is Karthick")
            assert pinged == []
        finally:
            cr.clinic_context_text = orig
