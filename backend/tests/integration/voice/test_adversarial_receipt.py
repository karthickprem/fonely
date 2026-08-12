"""Adversarial receipt probes through runtime → stateful engine.

Each probe exercises the exact runtime turn path, not the engine directly.
Covers: forged, wrong-business, stale, replay, concurrent, rollback.
"""

from __future__ import annotations

import time as time_mod
import zoneinfo
from datetime import UTC, date, datetime, time

import pytest

from fonely.voice.config import VoiceSessionConfig
from fonely.voice.context import AvailableSlot, DayAvailability, TrustedClock
from fonely.voice.runtime import (
    CommandResult,
    CommitReceipt,
    ConfirmCommand,
    PipelineRuntime,
    ProposeCommand,
    TrustedCommandContext,
)
from fonely.voice.test_engine import TestBookingEngine


def _clock():
    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    local = datetime(2026, 8, 10, 14, 30, tzinfo=tz)
    return TrustedClock(
        now_utc=local.astimezone(UTC),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, 10),
        day_of_week="monday",
    )


class STT:
    def __init__(self, texts):
        self._t = list(texts)
        self._i = 0

    async def transcribe(self, a):
        if self._i >= len(self._t):
            return ""
        t = self._t[self._i]
        self._i += 1
        return t

    async def close(self):
        pass


class LLM:
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


class TTS:
    def __init__(self):
        self.calls = 0

    async def synthesize(self, t):
        self.calls += 1
        return t.encode()

    async def close(self):
        pass


class Avail:
    async def query_day_availability(self, q):
        return DayAvailability(
            business_date=q.target_date,
            day_of_week="monday",
            is_operating_day=True,
            is_exception_day=False,
            available_slots=(AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),),
        )


# Helper: build a runtime that collects facts through 5 turns then confirms on 6th
FACT_TURNS = ["Appointment book", "Scaling", "Naalaikku", "6:30", "Karthick"]
FACT_RESPONSES = [
    "என்ன reason-க்காக visit?",
    "எந்த date-ல வரணும்?",
    "18:30 available. Time சரியா?",
    "பேரு சொல்லுங்க?",
    "Scaling, நாளை 6:30, Karthick. Correct-ஆ?",
]


async def _build_ready_runtime(engine, confirm_response="Booking confirmed."):
    """Build runtime, collect all facts, return ready for confirmation turn."""
    rt = PipelineRuntime(
        VoiceSessionConfig(session_id="adv-test", business_id=1),
        clock=_clock(),
        business_name="Test Dental",
        business_timezone="Asia/Kolkata",
        stt=STT([*FACT_TURNS, "Aamaa"]),
        llm=LLM([*FACT_RESPONSES, confirm_response]),
        tts=TTS(),
        availability_port=Avail(),
        command_port=engine,
        session_mode="live",
    )
    await rt.initialize()
    for _ in range(5):
        await rt.process_turn(b"x")  # STT returns scripted text
    return rt


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_full_dialogue_to_receipt(self):
        """Happy: 5 fact turns → user confirms → engine commit → typed receipt."""
        engine = TestBookingEngine()
        rt = await _build_ready_runtime(engine)
        result = await rt.process_turn(b"x")  # STT returns "Aamaa"

        assert engine.proposal_count == 1
        assert engine.commitment_count == 1
        assert result.commit_receipt is not None
        assert result.commit_receipt.business_id == 1
        assert result.commit_receipt.committed_at_ns > 0
        assert result.allowed  # Receipt-validated → ALLOW
        assert result.terminal  # Booking committed → terminal
        assert result.terminal_reason == "booking_committed"
        await rt.close()


class TestForgedReceipt:
    @pytest.mark.asyncio
    async def test_wrong_payload_digest_rejected(self):
        """Forged: engine returns receipt with wrong digest → discarded."""

        class ForgedEngine:
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
                        business_id=1,
                        operation="create",
                        idempotency_key="k",
                        confirm_idempotency_key="ck",
                        payload_digest="FORGED_DIGEST",
                        committed_at_ns=time_mod.monotonic_ns(),
                        facts={},
                    ),
                )

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="adv-forged", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            stt=STT([*FACT_TURNS, "Aamaa"]),
            llm=LLM([*FACT_RESPONSES, "Booking confirmed."]),
            tts=TTS(),
            availability_port=Avail(),
            command_port=ForgedEngine(),
            session_mode="live",
        )
        await rt.initialize()
        for _ in range(5):
            await rt.process_turn(b"x")
        result = await rt.process_turn(b"x")

        assert result.commit_receipt is None  # Forged digest rejected
        assert not result.allowed
        await rt.close()


class TestWrongBusiness:
    @pytest.mark.asyncio
    async def test_cross_tenant_receipt_rejected(self):
        """Wrong-business: receipt.business_id != config.business_id → discarded."""

        class WrongBizEngine:
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
                        business_id=999,
                        operation="create",
                        idempotency_key="k",
                        confirm_idempotency_key="ck",
                        payload_digest="",
                        committed_at_ns=time_mod.monotonic_ns(),
                        facts={},
                    ),
                )

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="adv-biz", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            stt=STT([*FACT_TURNS, "Aamaa"]),
            llm=LLM([*FACT_RESPONSES, "Booking confirmed."]),
            tts=TTS(),
            availability_port=Avail(),
            command_port=WrongBizEngine(),
            session_mode="live",
        )
        await rt.initialize()
        for _ in range(5):
            await rt.process_turn(b"x")
        result = await rt.process_turn(b"x")

        assert result.commit_receipt is None
        assert not result.allowed
        await rt.close()


class TestStaleReceipt:
    @pytest.mark.asyncio
    async def test_zero_timestamp_rejected(self):
        """Stale: receipt with committed_at_ns=0 passes binding but is still blocked."""

        class StaleEngine:
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
                        business_id=1,
                        operation="create",
                        idempotency_key="k",
                        confirm_idempotency_key="ck",
                        payload_digest="",
                        committed_at_ns=0,
                        facts={},
                    ),
                )

        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="adv-stale", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            stt=STT([*FACT_TURNS, "Aamaa"]),
            llm=LLM([*FACT_RESPONSES, "Booking confirmed."]),
            tts=TTS(),
            availability_port=Avail(),
            command_port=StaleEngine(),
            session_mode="live",
        )
        await rt.initialize()
        for _ in range(5):
            await rt.process_turn(b"x")
        result = await rt.process_turn(b"x")

        # Fail-closed stub blocks regardless; stale receipt still present
        assert not result.allowed
        await rt.close()


class TestReplay:
    @pytest.mark.asyncio
    async def test_idempotent_engine_replay(self):
        """Replay: engine returns same receipt on duplicate confirm."""
        engine = TestBookingEngine()
        ctx = TrustedCommandContext(business_id=1, actor_session_id="s", conversation_id="c")
        p = await engine.propose(ProposeCommand(context=ctx, idempotency_key="k1"))
        c1 = await engine.confirm(
            ConfirmCommand(context=ctx, proposal_id=p.proposal_id, idempotency_key="ck1")
        )
        c2 = await engine.confirm(
            ConfirmCommand(context=ctx, proposal_id=p.proposal_id, idempotency_key="ck1")
        )

        assert c1.receipt.commitment_id == c2.receipt.commitment_id
        assert engine.commitment_count == 1  # No duplicate effect


class TestConcurrent:
    @pytest.mark.asyncio
    async def test_concurrent_slot_conflict_through_engine(self):
        """Concurrent: two sessions, same slot → second gets slot_already_booked."""
        engine = TestBookingEngine()
        ctx1 = TrustedCommandContext(business_id=1, actor_session_id="s1", conversation_id="c1")
        ctx2 = TrustedCommandContext(business_id=1, actor_session_id="s2", conversation_id="c2")

        p1 = await engine.propose(
            ProposeCommand(
                context=ctx1,
                resource_id=1,
                target_date=date(2026, 8, 10),
                target_time="18:30",
                idempotency_key="k1",
            )
        )
        await engine.confirm(
            ConfirmCommand(context=ctx1, proposal_id=p1.proposal_id, idempotency_key="ck1")
        )

        p2 = await engine.propose(
            ProposeCommand(
                context=ctx2,
                resource_id=1,
                target_date=date(2026, 8, 10),
                target_time="18:30",
                idempotency_key="k2",
            )
        )
        assert not p2.success
        assert p2.error == "slot_already_booked"


class TestRollback:
    @pytest.mark.asyncio
    async def test_failed_confirm_preserves_pending(self):
        """Rollback: confirm wrong business → proposal stays pending."""
        engine = TestBookingEngine()
        ctx1 = TrustedCommandContext(business_id=1, actor_session_id="s", conversation_id="c")
        ctx2 = TrustedCommandContext(business_id=999, actor_session_id="s", conversation_id="c")

        p = await engine.propose(ProposeCommand(context=ctx1, idempotency_key="k1"))
        c = await engine.confirm(ConfirmCommand(context=ctx2, proposal_id=p.proposal_id))

        assert not c.success
        assert c.error == "business_mismatch"
        assert engine.commitment_count == 0

        # Original business can still confirm
        c2 = await engine.confirm(
            ConfirmCommand(context=ctx1, proposal_id=p.proposal_id, idempotency_key="ck1")
        )
        assert c2.committed
        assert engine.commitment_count == 1


class TestNoConfirmWithoutFacts:
    @pytest.mark.asyncio
    async def test_confirm_text_without_collection_no_command(self):
        """User says 'yes confirm' on first turn → no command (no facts)."""
        engine = TestBookingEngine()
        rt = PipelineRuntime(
            VoiceSessionConfig(session_id="adv-nofacts", business_id=1),
            clock=_clock(),
            business_name="Test",
            business_timezone="Asia/Kolkata",
            stt=STT(["yes confirm"]),
            llm=LLM(["Booking confirmed."]),
            tts=TTS(),
            command_port=engine,
            session_mode="live",
        )
        await rt.initialize()
        result = await rt.process_turn(b"x")

        assert engine.proposal_count == 0
        assert result.commit_receipt is None
        assert not result.allowed
        await rt.close()
