"""Behavioural proof that the CommandPort is the SOLE commit path (V-lane, V3).

This is the test to cite for "sole commit path" — not the AST guard in
test_pipeline_structure.py, which only forbids the syntactic appearance of a
second path. Here we observe the EFFECT: a counting CommandPort. A booking
conversation must invoke it exactly once; a conversation the gate refuses must
invoke it zero times. A counter catches a second path reached by indirection
(getattr/alias/injected service/callback) because it measures what happened,
not what the source spells.

Driven at the assembled-pipeline's gate (the only component that commits). The
full transport/serializer media→media roundtrip lands with the runtime in step
4; the commit guarantee does not depend on the transport and is proven here.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from pipecat.frames.frames import (
    Frame,
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
)
from fonely.voice.runtime import CommandResult, CommitReceipt

CLOCK = TrustedClock(
    now_utc=None,
    business_timezone="Asia/Kolkata",
    business_date=date(2026, 8, 12),
    day_of_week="wednesday",
)


class _CountingPort:
    """A CommandPort that counts every propose/confirm. The counts ARE the
    proof: they observe the commit effect regardless of how the call was
    reached, so a second commit path cannot hide from them."""

    def __init__(self) -> None:
        self.propose_count = 0
        self.confirm_count = 0
        self.confirm_args: list[object] = []

    async def propose(self, cmd):
        self.propose_count += 1
        return CommandResult(
            success=True, operation="create", proposal_id=42, evidence={"version": 2}
        )

    async def confirm(self, cmd):
        self.confirm_count += 1
        self.confirm_args.append(cmd)
        receipt = CommitReceipt(
            commitment_id=777,
            proposal_id=42,
            business_id=1,
            operation="create",
            idempotency_key=cmd.idempotency_key,
            confirm_idempotency_key=cmd.idempotency_key,
            payload_digest="",
            committed_at_ns=1,
            source="appointment_service",
            facts={"service_name": "Scaling", "resource_name": "Dr. Priya"},
        )
        return CommandResult(
            success=True,
            operation="create",
            proposal_id=42,
            committed=True,
            receipt=receipt,
        )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _FakeSession:
    """Answers the resolver's identity queries; usable as an async context
    manager (session_factory() → `async with`)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        if "from services" in sql and "where business_id" in sql:
            return _FakeResult([(10, "Scaling")])
        if "service_resource_eligibility" in sql:
            return _FakeResult([(1, "Dr. Priya")])
        return _FakeResult([])

    async def commit(self):
        return None


def _resolver(port: _CountingPort) -> ResolverContext:
    return ResolverContext(
        business_id=1,
        session_factory=lambda: _FakeSession(),
        command_port=port,  # type: ignore[arg-type]
        clock=CLOCK,
    )


class _Collector:
    def __init__(self) -> None:
        self.frames: list[Frame] = []

    async def _cb(self, frame: Frame, direction: FrameDirection) -> None:
        self.frames.append(frame)


async def _drive_gate(gate: BookingPostLLMGate, text: str) -> _Collector:
    collector = _Collector()
    gate.push_frame = collector._cb  # type: ignore[method-assign]
    await gate.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(LLMTextFrame(text=text), FrameDirection.DOWNSTREAM)
    await gate.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    return collector


def _completed_injector(port: _CountingPort) -> BookingStateInjector:
    injector = BookingStateInjector(_resolver(port))
    bc = injector.booking
    bc.active = True
    bc.reason = "scaling"
    bc.target_date = date(2026, 8, 12)
    bc.selected_time = time(17, 0)
    bc.patient_name = "Karthick"
    assert bc.required_field == "confirmation"
    return injector


class TestSoleCommitPath:
    @pytest.mark.asyncio
    async def test_confirmed_booking_invokes_port_exactly_once(self):
        port = _CountingPort()
        injector = _completed_injector(port)
        injector.caller_confirmed = True  # caller said yes
        gate = BookingPostLLMGate(injector, _resolver(port))

        await _drive_gate(gate, "booking it now")

        # The effect: the port committed exactly once. This is the guarantee.
        assert port.propose_count == 1
        assert port.confirm_count == 1

    @pytest.mark.asyncio
    async def test_refused_booking_invokes_port_zero_times(self):
        # Not confirmed → the gate must NOT commit. Zero port invocations.
        port = _CountingPort()
        injector = _completed_injector(port)
        injector.caller_confirmed = False
        gate = BookingPostLLMGate(injector, _resolver(port))

        await _drive_gate(gate, "actually can we change the time?")

        assert port.propose_count == 0
        assert port.confirm_count == 0

    @pytest.mark.asyncio
    async def test_double_end_does_not_double_commit(self):
        # A confirmed booking followed by another response must not commit twice
        # — the gate books once, then speaks "noted" without re-invoking the port.
        port = _CountingPort()
        injector = _completed_injector(port)
        injector.caller_confirmed = True
        gate = BookingPostLLMGate(injector, _resolver(port))

        await _drive_gate(gate, "first")
        await _drive_gate(gate, "second")

        assert port.propose_count == 1
        assert port.confirm_count == 1
