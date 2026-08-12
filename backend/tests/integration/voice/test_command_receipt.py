"""Vertical test: runtime → engine → typed receipt → speech blocked.

Tests traverse the PipelineRuntime, not the engine directly.
Proves: consequential speech + CommandPort → typed CommitReceipt →
receipt validated → speech still BLOCKED by fail-closed stub.
"""

from __future__ import annotations

import time as time_mod
from datetime import UTC, date, datetime, time

import pytest

from fonely.voice.config import VoiceSessionConfig
from fonely.voice.context import AvailableSlot, DayAvailability, TrustedClock
from fonely.voice.runtime import (
    CommandResult,
    CommitReceipt,
    PipelineRuntime,
)
from fonely.voice.test_engine import TestBookingEngine


def _clock():
    import zoneinfo

    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    local = datetime(2026, 8, 10, 14, 30, tzinfo=tz)
    return TrustedClock(
        now_utc=local.astimezone(UTC),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, 10),
        day_of_week="monday",
    )


class SimpleSTT:
    async def transcribe(self, a):
        return a.decode("utf-8", errors="replace")

    async def close(self):
        pass


class SimpleLLM:
    def __init__(self, responses):
        self._r = list(responses)
        self._i = 0

    async def generate(self, s, m):
        if self._i >= len(self._r):
            return ""
        r = self._r[self._i]
        self._i += 1
        return r

    async def close(self):
        pass


class SimpleTTS:
    def __init__(self):
        self.calls = 0

    async def synthesize(self, t):
        self.calls += 1
        return t.encode("utf-8")

    async def close(self):
        pass


class SimpleAvail:
    async def query_day_availability(self, q):
        return DayAvailability(
            business_date=q.target_date,
            day_of_week="monday",
            is_operating_day=True,
            is_exception_day=False,
            available_slots=(AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),),
        )


class TestRuntimeToEngineReceipt:
    @pytest.mark.asyncio
    async def test_receipt_obtained_after_full_dialogue(self):
        """Full dialogue: collect facts → readback → user confirms → receipt."""
        engine = TestBookingEngine()

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="rt-eng-1", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=SimpleSTT(),
            llm=SimpleLLM(
                [
                    "என்ன reason-க்காக visit?",  # asks reason
                    "எந்த date-ல வரணும்?",  # asks date
                    "18:30 available. Time சரியா?",  # asks time
                    "பேரு சொல்லுங்க?",  # asks name
                    "Scaling, நாளை 6:30, Karthick. Correct-ஆ?",  # readback
                    "Booking confirmed for 6:30.",  # confirmation response
                ]
            ),
            tts=SimpleTTS(),
            availability_port=SimpleAvail(),
            command_port=engine,
            session_mode="live",
        )
        await rt.initialize()

        # Simulate multi-turn dialogue collecting facts
        await rt.process_turn(b"Appointment book pannanum")  # reason asked
        await rt.process_turn(b"Scaling")  # date asked
        await rt.process_turn(b"Naalaikku")  # time asked
        await rt.process_turn(b"6:30")  # name asked
        await rt.process_turn(b"Karthick")  # readback

        # User explicitly confirms after readback
        result = await rt.process_turn(b"Aamaa, confirm pannunga")

        # Engine was invoked only after user confirmation
        assert engine.proposal_count == 1
        assert engine.commitment_count == 1

        # Typed receipt obtained
        assert result.commit_receipt is not None
        assert isinstance(result.commit_receipt, CommitReceipt)
        assert result.commit_receipt.business_id == 1

        # Receipt-validated → ALLOW
        assert result.allowed
        assert result.terminal
        assert result.terminal_reason == "booking_committed"

        await rt.close()

    @pytest.mark.asyncio
    async def test_no_engine_no_receipt(self):
        """Without CommandPort: no receipt, speech blocked."""
        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="rt-eng-2", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=SimpleSTT(),
            llm=SimpleLLM(["Booking confirmed for 6:30."]),
            tts=SimpleTTS(),
            session_mode="demo",
        )
        await rt.initialize()
        result = await rt.process_turn(b"Confirm booking")
        assert result.commit_receipt is None
        assert not result.allowed
        await rt.close()

    @pytest.mark.asyncio
    async def test_no_command_on_casual_confirm_text(self):
        """User says 'confirm' without prior fact collection → no command."""
        engine = TestBookingEngine()

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="rt-eng-3", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=SimpleSTT(),
            llm=SimpleLLM(["Booking confirmed."]),
            tts=SimpleTTS(),
            command_port=engine,
            session_mode="live",
        )
        await rt.initialize()
        r1 = await rt.process_turn(b"Confirm")

        # No facts collected, no readback → no command invoked
        assert engine.proposal_count == 0
        assert r1.commit_receipt is None
        assert not r1.allowed  # Consequential speech blocked

        await rt.close()

    @pytest.mark.asyncio
    async def test_wrong_business_receipt_rejected(self):
        """Receipt from wrong business_id is discarded by runtime validation."""

        class WrongBusinessEngine:
            async def propose(self, cmd):
                return CommandResult(success=True, operation="create", proposal_id=1)

            async def confirm(self, cmd):
                return CommandResult(
                    success=True,
                    operation="create",
                    proposal_id=1,
                    committed=True,
                    receipt=CommitReceipt(
                        commitment_id=1,
                        proposal_id=1,
                        business_id=999,  # WRONG
                        operation="create",
                        idempotency_key="k1",
                        confirm_idempotency_key="ck1",
                        payload_digest="",
                        committed_at_ns=time_mod.monotonic_ns(),
                        facts={},
                    ),
                )

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="rt-eng-4", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=SimpleSTT(),
            llm=SimpleLLM(["Booking confirmed."]),
            tts=SimpleTTS(),
            command_port=WrongBusinessEngine(),
            session_mode="live",
        )
        await rt.initialize()
        result = await rt.process_turn(b"Confirm")

        # Wrong business receipt discarded
        assert result.commit_receipt is None
        assert not result.allowed
        await rt.close()
