"""Tests for the in-memory agent↔doctor bridge."""

from __future__ import annotations

import asyncio

import pytest

from fonely.voice.doctor_bridge import InMemoryDoctorBridge


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_ask_notifies_registered_ws():
    bridge = InMemoryDoctorBridge()
    ws = _FakeWS()
    bridge.register_ws(ws)

    await bridge.ask_doctor("What are your slots?", "patient asked about today")

    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "agent_question"
    assert ws.sent[0]["query_id"] == 0


@pytest.mark.asyncio
async def test_ask_wait_respond_cycle():
    bridge = InMemoryDoctorBridge()
    query = await bridge.ask_doctor("slots?")

    async def respond_soon():
        await asyncio.sleep(0.05)
        bridge.doctor_responds(0, "5pm to 7pm")

    responder = asyncio.create_task(respond_soon())
    response = await bridge.wait_for_response(query, timeout=2.0)
    await responder
    assert response == "5pm to 7pm"


@pytest.mark.asyncio
async def test_timeout_returns_none():
    bridge = InMemoryDoctorBridge()
    query = await bridge.ask_doctor("slots?")
    response = await bridge.wait_for_response(query, timeout=0.05)
    assert response is None


@pytest.mark.asyncio
async def test_dead_ws_does_not_break_ask():
    class _DeadWS:
        async def send_json(self, payload):
            raise ConnectionError("socket closed")

    bridge = InMemoryDoctorBridge()
    bridge.register_ws(_DeadWS())
    # Must not raise — a dead socket cannot break the agent's turn.
    query = await bridge.ask_doctor("slots?")
    assert query.question == "slots?"


@pytest.mark.asyncio
async def test_double_response_rejected():
    bridge = InMemoryDoctorBridge()
    await bridge.ask_doctor("slots?")
    assert bridge.doctor_responds(0, "first") is True
    assert bridge.doctor_responds(0, "second") is False


@pytest.mark.asyncio
async def test_respond_to_unknown_query_id():
    bridge = InMemoryDoctorBridge()
    assert bridge.doctor_responds(99, "x") is False


@pytest.mark.asyncio
async def test_pending_queries_snapshot():
    bridge = InMemoryDoctorBridge()
    await bridge.ask_doctor("q1", "ctx1")
    bridge.doctor_responds(0, "answered")
    await bridge.ask_doctor("q2")

    pending = bridge.pending_queries
    assert len(pending) == 2
    assert pending[0]["answered"] is True
    assert pending[0]["response"] == "answered"
    assert pending[1]["answered"] is False
