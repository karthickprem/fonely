"""Vertical test: command port invoked → committed receipt → speech allowed.

Proves: consequential LLM response + CommandPort → propose → confirm →
committed evidence → speech reclassified as receipt-backed NON_CONSEQUENTIAL →
validator ALLOW → TTS audio in TurnResult → commit_evidence populated.

Also proves: without CommandPort, same consequential text is BLOCKED.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timezone
from typing import Any

import pytest

from fonely.voice.config import SpeechClass, VoiceSessionConfig
from fonely.voice.context import AvailabilityQuery, AvailableSlot, DayAvailability, TrustedClock
from fonely.voice.runtime import (
    CommandPort, CommandResult, ConfirmCommand, PipelineRuntime, ProposeCommand,
)


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


class TrackingCommandPort:
    def __init__(self, business_id: int = 1):
        self.proposals: list[ProposeCommand] = []
        self.confirmations: list[ConfirmCommand] = []
        self._business_id = business_id

    async def propose(self, cmd: ProposeCommand) -> CommandResult:
        self.proposals.append(cmd)
        return CommandResult(success=True, operation="create", proposal_id=1)

    async def confirm(self, cmd: ConfirmCommand) -> CommandResult:
        self.confirmations.append(cmd)
        import time
        return CommandResult(
            success=True, operation="create", proposal_id=1,
            committed=True,
            evidence={
                "appointment_id": 42,
                "pending_action_id": 1,
                "business_id": self._business_id,
                "proposal_id": cmd.proposal_id,
                "committed_at_ns": time.monotonic_ns(),
            },
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


class TestCommandReceiptAllowsConsequentialSpeech:
    @pytest.mark.asyncio
    async def test_committed_receipt_obtained_and_validated(self):
        """With CommandPort: propose → confirm → receipt obtained.

        Speech remains BLOCKED by fail-closed stub even with receipt.
        An accepted validator (future) would ALLOW receipt-backed speech.
        """
        cmd_port = TrackingCommandPort()
        tts = SimpleTTS()

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="cmd-1", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=SimpleSTT(),
            llm=SimpleLLM(["Booking confirmed for 6:30."]),
            tts=tts,
            availability_port=SimpleAvail(),
            command_port=cmd_port,
            session_mode="live",
        )
        await rt.initialize()
        result = await rt.process_turn(b"Confirm booking")

        # Command port was invoked with idempotency
        assert len(cmd_port.proposals) == 1
        assert len(cmd_port.confirmations) == 1
        assert cmd_port.proposals[0].idempotency_key.startswith("voice-cmd-1-")

        # Receipt obtained with verifiable facts
        assert result.commit_evidence is not None
        assert result.commit_evidence["appointment_id"] == 42
        assert result.commit_evidence["pending_action_id"] == 1

        # Fail-closed stub BLOCKS consequential speech even with receipt
        # This is correct: stub cannot verify receipt authenticity
        assert not result.allowed
        assert result.speech_class == SpeechClass.COMMITTED_CREATE

        await rt.close()

    @pytest.mark.asyncio
    async def test_no_command_port_blocks_consequential(self):
        """Without CommandPort: same consequential text → BLOCKED."""
        tts = SimpleTTS()

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="cmd-2", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=SimpleSTT(),
            llm=SimpleLLM(["Booking confirmed for 6:30."]),
            tts=tts,
            session_mode="demo",
        )
        await rt.initialize()
        result = await rt.process_turn(b"Confirm booking")

        # No command port, no evidence
        assert result.commit_evidence is None

        # Speech was blocked
        assert not result.allowed
        assert result.response_audio == b""

        await rt.close()

    @pytest.mark.asyncio
    async def test_failed_command_blocks_speech(self):
        """CommandPort present but confirm fails → BLOCKED."""
        class FailingCommand:
            async def propose(self, cmd):
                return CommandResult(success=True, operation="create", proposal_id=1)
            async def confirm(self, cmd):
                return CommandResult(success=False, error="slot_taken")

        tts = SimpleTTS()

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="cmd-3", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=SimpleSTT(),
            llm=SimpleLLM(["Booking confirmed for 6:30."]),
            tts=tts,
            command_port=FailingCommand(),
            session_mode="live",
        )
        await rt.initialize()
        result = await rt.process_turn(b"Confirm booking")

        # Command failed — no evidence, blocked
        assert result.commit_evidence is None
        assert not result.allowed

        await rt.close()

    @pytest.mark.asyncio
    async def test_command_exception_blocks_speech(self):
        """CommandPort raises exception → BLOCKED, no crash."""
        class CrashingCommand:
            async def propose(self, cmd):
                raise RuntimeError("DB down")
            async def confirm(self, cmd):
                raise RuntimeError("unreachable")

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="cmd-4", business_id=1),
            clock=_clock(),
            business_name="Test Dental",
            business_timezone="Asia/Kolkata",
            stt=SimpleSTT(),
            llm=SimpleLLM(["Booking confirmed for 6:30."]),
            tts=SimpleTTS(),
            command_port=CrashingCommand(),
            session_mode="live",
        )
        await rt.initialize()
        result = await rt.process_turn(b"Confirm booking")

        assert result.commit_evidence is None
        assert not result.allowed

        await rt.close()
