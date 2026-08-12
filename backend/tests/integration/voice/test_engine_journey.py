"""Vertical journey through TestBookingEngine with trusted context and typed receipts.

All commands carry TrustedCommandContext. Tests traverse the engine
through runtime-compatible ProposeCommand/ConfirmCommand with
idempotency, payload digest, slot conflicts, and receipt validation.
"""

from __future__ import annotations

from datetime import date

import pytest

from fonely.voice.runtime import (
    CommitReceipt,
    ConfirmCommand,
    ProposeCommand,
    TrustedCommandContext,
)
from fonely.voice.test_engine import TestBookingEngine


def _ctx(business_id=1, session="s1"):
    return TrustedCommandContext(
        business_id=business_id,
        actor_session_id=session,
        conversation_id=session,
        booking_attempt=1,
    )


class TestEngineState:
    @pytest.mark.asyncio
    async def test_propose_creates_state(self):
        engine = TestBookingEngine()
        result = await engine.propose(
            ProposeCommand(
                context=_ctx(),
                service_id=10,
                resource_id=1,
                target_date=date(2026, 8, 10),
                target_time="18:30",
                customer_name="Karthick",
                idempotency_key="voice-s1-t1",
            )
        )
        assert result.success
        assert result.proposal_id == 1
        assert engine.proposal_count == 1

    @pytest.mark.asyncio
    async def test_confirm_returns_typed_receipt(self):
        engine = TestBookingEngine()
        p = await engine.propose(
            ProposeCommand(
                context=_ctx(),
                service_id=10,
                resource_id=1,
                target_date=date(2026, 8, 10),
                target_time="18:30",
                customer_name="Karthick",
                idempotency_key="voice-s1-t1",
            )
        )
        c = await engine.confirm(
            ConfirmCommand(
                context=_ctx(),
                proposal_id=p.proposal_id,
                idempotency_key="voice-s1-t1-confirm",
            )
        )
        assert c.committed
        assert c.receipt is not None
        assert isinstance(c.receipt, CommitReceipt)
        assert c.receipt.business_id == 1
        assert c.receipt.proposal_id == p.proposal_id
        assert c.receipt.commitment_id == 1
        assert c.receipt.committed_at_ns > 0
        assert c.receipt.facts["customer_name"] == "Karthick"
        assert c.receipt.facts["target_time"] == "18:30"
        assert engine.commitment_count == 1


class TestEngineIdempotency:
    @pytest.mark.asyncio
    async def test_same_key_returns_same_proposal(self):
        engine = TestBookingEngine()
        p1 = await engine.propose(ProposeCommand(context=_ctx(), idempotency_key="k1"))
        p2 = await engine.propose(ProposeCommand(context=_ctx(), idempotency_key="k1"))
        assert p1.proposal_id == p2.proposal_id
        assert engine.proposal_count == 1

    @pytest.mark.asyncio
    async def test_different_business_same_key_rejected(self):
        engine = TestBookingEngine()
        await engine.propose(ProposeCommand(context=_ctx(business_id=1), idempotency_key="k1"))
        p2 = await engine.propose(ProposeCommand(context=_ctx(business_id=2), idempotency_key="k1"))
        assert not p2.success
        assert p2.error == "idempotency_business_mismatch"

    @pytest.mark.asyncio
    async def test_double_confirm_returns_same_receipt(self):
        engine = TestBookingEngine()
        p = await engine.propose(ProposeCommand(context=_ctx(), idempotency_key="k1"))
        c1 = await engine.confirm(
            ConfirmCommand(context=_ctx(), proposal_id=p.proposal_id, idempotency_key="ck1")
        )
        c2 = await engine.confirm(
            ConfirmCommand(context=_ctx(), proposal_id=p.proposal_id, idempotency_key="ck1")
        )
        assert c1.committed and c2.committed
        assert c1.receipt.commitment_id == c2.receipt.commitment_id
        assert engine.commitment_count == 1


class TestEngineSlotConflicts:
    @pytest.mark.asyncio
    async def test_double_booking_rejected(self):
        engine = TestBookingEngine()
        p1 = await engine.propose(
            ProposeCommand(
                context=_ctx(),
                resource_id=1,
                target_date=date(2026, 8, 10),
                target_time="18:30",
                idempotency_key="k1",
            )
        )
        await engine.confirm(
            ConfirmCommand(context=_ctx(), proposal_id=p1.proposal_id, idempotency_key="ck1")
        )
        p2 = await engine.propose(
            ProposeCommand(
                context=_ctx(),
                resource_id=1,
                target_date=date(2026, 8, 10),
                target_time="18:30",
                idempotency_key="k2",
            )
        )
        assert not p2.success
        assert p2.error == "slot_already_booked"

    @pytest.mark.asyncio
    async def test_different_slot_succeeds(self):
        engine = TestBookingEngine()
        p1 = await engine.propose(
            ProposeCommand(
                context=_ctx(),
                resource_id=1,
                target_date=date(2026, 8, 10),
                target_time="18:30",
                idempotency_key="k1",
            )
        )
        await engine.confirm(
            ConfirmCommand(context=_ctx(), proposal_id=p1.proposal_id, idempotency_key="ck1")
        )
        p2 = await engine.propose(
            ProposeCommand(
                context=_ctx(),
                resource_id=1,
                target_date=date(2026, 8, 10),
                target_time="10:00",
                idempotency_key="k2",
            )
        )
        assert p2.success


class TestEngineValidation:
    @pytest.mark.asyncio
    async def test_confirm_wrong_business_rejected(self):
        engine = TestBookingEngine()
        p = await engine.propose(ProposeCommand(context=_ctx(business_id=1), idempotency_key="k1"))
        c = await engine.confirm(
            ConfirmCommand(context=_ctx(business_id=999), proposal_id=p.proposal_id)
        )
        assert not c.success
        assert c.error == "business_mismatch"

    @pytest.mark.asyncio
    async def test_confirm_nonexistent_rejected(self):
        engine = TestBookingEngine()
        c = await engine.confirm(ConfirmCommand(context=_ctx(), proposal_id=999))
        assert not c.success
        assert c.error == "proposal_not_found"

    @pytest.mark.asyncio
    async def test_receipt_bound_to_request_facts(self):
        engine = TestBookingEngine()
        p = await engine.propose(
            ProposeCommand(
                context=_ctx(),
                service_id=10,
                resource_id=1,
                target_date=date(2026, 8, 10),
                target_time="18:30",
                customer_name="Priya",
                idempotency_key="k1",
                payload_digest="abc123",
            )
        )
        c = await engine.confirm(
            ConfirmCommand(
                context=_ctx(),
                proposal_id=p.proposal_id,
                idempotency_key="ck1",
            )
        )
        assert c.receipt.facts["service_id"] == 10
        assert c.receipt.facts["target_time"] == "18:30"
        assert c.receipt.facts["customer_name"] == "Priya"
        assert c.receipt.idempotency_key == "k1"
        assert c.receipt.confirm_idempotency_key == "ck1"
        assert c.receipt.payload_digest == "abc123"
