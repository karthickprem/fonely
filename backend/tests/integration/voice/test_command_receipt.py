"""Vertical test: runtime → engine → typed receipt → speech blocked.

Tests traverse the PipelineRuntime, not the engine directly.
Proves: consequential speech + CommandPort → typed CommitReceipt →
receipt validated → speech still BLOCKED by fail-closed stub.
"""
from __future__ import annotations

import time as time_mod
from datetime import date, datetime, time, timezone

import pytest

from fonely.voice.config import SpeechClass, VoiceSessionConfig
from fonely.voice.context import AvailabilityQuery, AvailableSlot, DayAvailability, TrustedClock
from fonely.voice.runtime import (
    CommandResult, CommitReceipt, ConfirmCommand, PipelineRuntime,
    ProposeCommand, TrustedCommandContext,
)
from fonely.voice.test_engine import TestBookingEngine


def _clock():
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    local = datetime(2026, 8, 10, 14, 30, tzinfo=tz)
    return TrustedClock(
        now_utc=local.astimezone(timezone.utc),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, 10),
        day_of_week="monday",
    )


class SimpleSTT:
    async def transcribe(self, a): return a.decode("utf-8", errors="replace")
    async def close(self): pass

class SimpleLLM:
    def __init__(self, responses):
        self._r = list(responses); self._i = 0
    async def generate(self, s, m):
        if self._i >= len(self._r): return ""
        r = self._r[self._i]; self._i += 1; return r
    async def close(self): pass

class SimpleTTS:
    def __init__(self): self.calls = 0
    async def synthesize(self, t):
        self.calls += 1; return t.encode("utf-8")
    async def close(self): pass

class SimpleAvail:
    async def query_day_availability(self, q):
        return DayAvailability(
            business_date=q.target_date, day_of_week="monday",
            is_operating_day=True, is_exception_day=False,
            available_slots=(AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),),
        )


class TestRuntimeToEngineReceipt:
    @pytest.mark.asyncio
    async def test_receipt_obtained_through_runtime(self):
        """Runtime → TestBookingEngine → typed CommitReceipt obtained."""
        engine = TestBookingEngine()

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="rt-eng-1", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=SimpleSTT(),
            llm=SimpleLLM(["Booking confirmed for 6:30."]),
            tts=SimpleTTS(),
            availability_port=SimpleAvail(),
            command_port=engine,
            session_mode="live",
        )
        await rt.initialize()
        result = await rt.process_turn(b"Confirm booking")

        # Engine was invoked through runtime
        assert engine.proposal_count == 1
        assert engine.commitment_count == 1

        # Typed receipt obtained
        assert result.commit_receipt is not None
        assert isinstance(result.commit_receipt, CommitReceipt)
        assert result.commit_receipt.business_id == 1
        assert result.commit_receipt.commitment_id == 1
        assert result.commit_receipt.committed_at_ns > 0

        # Speech still BLOCKED by fail-closed stub
        assert not result.allowed
        assert result.speech_class == SpeechClass.COMMITTED_CREATE

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
    async def test_first_turn_commits_and_subsequent_safe(self):
        """First turn commits; second turn is non-consequential and allowed."""
        engine = TestBookingEngine()

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="rt-eng-3", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=SimpleSTT(),
            llm=SimpleLLM(["Booking confirmed.", "Thanks for booking!"]),
            tts=SimpleTTS(),
            command_port=engine,
            session_mode="live",
        )
        await rt.initialize()
        r1 = await rt.process_turn(b"Confirm")

        # First turn gets receipt through engine
        assert r1.commit_receipt is not None
        assert engine.proposal_count == 1
        assert engine.commitment_count == 1

        r2 = await rt.process_turn(b"Thanks")
        # Second turn is non-consequential (safe pattern)
        assert r2.commit_receipt is None  # No command invoked
        assert r2.speech_class == SpeechClass.NON_CONSEQUENTIAL

        await rt.close()

    @pytest.mark.asyncio
    async def test_wrong_business_receipt_rejected(self):
        """Receipt from wrong business_id is discarded by runtime validation."""
        class WrongBusinessEngine:
            async def propose(self, cmd):
                return CommandResult(success=True, operation="create", proposal_id=1)
            async def confirm(self, cmd):
                return CommandResult(
                    success=True, operation="create", proposal_id=1,
                    committed=True,
                    receipt=CommitReceipt(
                        commitment_id=1, proposal_id=1,
                        business_id=999,  # WRONG
                        operation="create",
                        idempotency_key="k1", confirm_idempotency_key="ck1",
                        payload_digest="", committed_at_ns=time_mod.monotonic_ns(),
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
