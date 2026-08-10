"""Vertical journey through TestBookingEngine with real state and idempotency.

Proves: real engine maintains state, enforces idempotency, validates
targets, produces verifiable typed receipts, prevents double-booking,
and receipt is bound to request facts.
"""
from __future__ import annotations

from datetime import date

import pytest

from fonely.voice.runtime import ProposeCommand, ConfirmCommand
from fonely.voice.test_engine import TestBookingEngine


class TestEngineState:
    @pytest.mark.asyncio
    async def test_propose_creates_state(self):
        engine = TestBookingEngine()
        result = await engine.propose(ProposeCommand(
            business_id=1, service_id=10, resource_id=1,
            target_date=date(2026, 8, 10), target_time="18:30",
            customer_name="Karthick", idempotency_key="voice-s1-t1",
        ))
        assert result.success
        assert result.proposal_id == 1
        assert engine.proposal_count == 1

    @pytest.mark.asyncio
    async def test_confirm_commits_and_returns_receipt(self):
        engine = TestBookingEngine()
        p = await engine.propose(ProposeCommand(
            business_id=1, service_id=10, resource_id=1,
            target_date=date(2026, 8, 10), target_time="18:30",
            customer_name="Karthick", idempotency_key="voice-s1-t1",
        ))
        c = await engine.confirm(ConfirmCommand(
            business_id=1, proposal_id=p.proposal_id,
            idempotency_key="voice-s1-t1-confirm",
        ))
        assert c.committed
        assert c.evidence is not None
        assert c.evidence["business_id"] == 1
        assert c.evidence["proposal_id"] == p.proposal_id
        assert c.evidence["customer_name"] == "Karthick"
        assert c.evidence["target_time"] == "18:30"
        assert c.evidence["commitment_id"] == 1
        assert c.evidence["committed_at_ns"] > 0
        assert engine.commitment_count == 1


class TestEngineIdempotency:
    @pytest.mark.asyncio
    async def test_same_key_returns_same_proposal(self):
        engine = TestBookingEngine()
        p1 = await engine.propose(ProposeCommand(
            business_id=1, idempotency_key="key-1",
        ))
        p2 = await engine.propose(ProposeCommand(
            business_id=1, idempotency_key="key-1",
        ))
        assert p1.proposal_id == p2.proposal_id
        assert engine.proposal_count == 1

    @pytest.mark.asyncio
    async def test_different_business_same_key_rejected(self):
        engine = TestBookingEngine()
        await engine.propose(ProposeCommand(
            business_id=1, idempotency_key="key-1",
        ))
        p2 = await engine.propose(ProposeCommand(
            business_id=2, idempotency_key="key-1",
        ))
        assert not p2.success
        assert p2.error == "idempotency_business_mismatch"

    @pytest.mark.asyncio
    async def test_double_confirm_returns_same_commitment(self):
        engine = TestBookingEngine()
        p = await engine.propose(ProposeCommand(business_id=1, idempotency_key="k1"))
        c1 = await engine.confirm(ConfirmCommand(business_id=1, proposal_id=p.proposal_id, idempotency_key="ck1"))
        c2 = await engine.confirm(ConfirmCommand(business_id=1, proposal_id=p.proposal_id, idempotency_key="ck1"))
        assert c1.committed and c2.committed
        assert c1.evidence["commitment_id"] == c2.evidence["commitment_id"]
        assert engine.commitment_count == 1


class TestEngineSlotConflicts:
    @pytest.mark.asyncio
    async def test_double_booking_rejected(self):
        engine = TestBookingEngine()
        p1 = await engine.propose(ProposeCommand(
            business_id=1, resource_id=1,
            target_date=date(2026, 8, 10), target_time="18:30",
            idempotency_key="k1",
        ))
        await engine.confirm(ConfirmCommand(business_id=1, proposal_id=p1.proposal_id, idempotency_key="ck1"))

        # Same slot should conflict
        p2 = await engine.propose(ProposeCommand(
            business_id=1, resource_id=1,
            target_date=date(2026, 8, 10), target_time="18:30",
            idempotency_key="k2",
        ))
        assert not p2.success
        assert p2.error == "slot_already_booked"

    @pytest.mark.asyncio
    async def test_different_slot_succeeds(self):
        engine = TestBookingEngine()
        p1 = await engine.propose(ProposeCommand(
            business_id=1, resource_id=1,
            target_date=date(2026, 8, 10), target_time="18:30",
            idempotency_key="k1",
        ))
        await engine.confirm(ConfirmCommand(business_id=1, proposal_id=p1.proposal_id, idempotency_key="ck1"))

        # Different time succeeds
        p2 = await engine.propose(ProposeCommand(
            business_id=1, resource_id=1,
            target_date=date(2026, 8, 10), target_time="10:00",
            idempotency_key="k2",
        ))
        assert p2.success


class TestEngineValidation:
    @pytest.mark.asyncio
    async def test_confirm_wrong_business_rejected(self):
        engine = TestBookingEngine()
        p = await engine.propose(ProposeCommand(business_id=1, idempotency_key="k1"))
        c = await engine.confirm(ConfirmCommand(business_id=999, proposal_id=p.proposal_id))
        assert not c.success
        assert c.error == "business_mismatch"

    @pytest.mark.asyncio
    async def test_confirm_nonexistent_proposal_rejected(self):
        engine = TestBookingEngine()
        c = await engine.confirm(ConfirmCommand(business_id=1, proposal_id=999))
        assert not c.success
        assert c.error == "proposal_not_found"

    @pytest.mark.asyncio
    async def test_receipt_bound_to_request_facts(self):
        engine = TestBookingEngine()
        p = await engine.propose(ProposeCommand(
            business_id=1, service_id=10, resource_id=1,
            target_date=date(2026, 8, 10), target_time="18:30",
            customer_name="Priya", idempotency_key="k1",
        ))
        c = await engine.confirm(ConfirmCommand(business_id=1, proposal_id=p.proposal_id, idempotency_key="ck1"))

        # Receipt contains exactly the proposed facts
        assert c.evidence["service_id"] == 10
        assert c.evidence["resource_id"] == 1
        assert c.evidence["target_date"] == "2026-08-10"
        assert c.evidence["target_time"] == "18:30"
        assert c.evidence["customer_name"] == "Priya"
        assert c.evidence["idempotency_key"] == "k1"
        assert c.evidence["confirm_idempotency_key"] == "ck1"
