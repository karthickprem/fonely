"""#45(b) unit: the appointment command's call_id comes ONLY from the port's
construction (the admitted session), never from caller/model data — and the
threading is non-vacuous (a None-construction produces a None call_id).

No DB: AppointmentService.create_proposal is patched to CAPTURE the
CreatePendingAppointmentCommand, so we assert the call_id the port put on it.
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import pytest

from fonely.voice.backend_ports import AppointmentServiceCommandPort, build_actor_context
from fonely.voice.runtime import ProposeCommand, TrustedCommandContext


class _CapturingService:
    """Stands in for AppointmentService: records the command, returns a minimal
    successful proposal so propose() completes."""

    captured: ClassVar[list] = []

    def __init__(self, session, *, validation):
        pass

    async def create_proposal(self, command):
        _CapturingService.captured.append(command)

        class _Res:
            pending_action_id = 42
            version = 2

        return _Res()


@pytest.fixture(autouse=True)
def _patch_service(monkeypatch):
    _CapturingService.captured = []
    import fonely.services.appointments as appts

    monkeypatch.setattr(appts, "AppointmentService", _CapturingService)
    yield


class _NullSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass


def _port(*, call_id):
    actor = build_actor_context(business_id=1, phone="+919000000000", session_id="s1")
    return AppointmentServiceCommandPort(
        actor=actor,
        session_factory=lambda: _NullSession(),
        validation_factory=lambda s: object(),
        business_timezone="Asia/Kolkata",
        conversation_id="conv-1",
        call_id=call_id,
    )


def _propose_cmd():
    return ProposeCommand(
        context=TrustedCommandContext(
            business_id=1, actor_session_id="s1", conversation_id="conv-1"
        ),
        service_id=1,
        resource_id=1,
        target_date=date(2026, 8, 18),
        target_time="18:30",
        idempotency_key="k1",
    )


class TestCallIdTrustSource:
    @pytest.mark.asyncio
    async def test_call_id_comes_from_construction_not_the_propose_command(self):
        # ProposeCommand has NO call_id field to supply — the port injects its own
        # constructor value. Confirm the captured command carries the port's id.
        port = _port(call_id=777)
        await port.propose(_propose_cmd())

        assert len(_CapturingService.captured) == 1
        assert _CapturingService.captured[0].call_id == 777  # from construction

    @pytest.mark.asyncio
    async def test_none_construction_yields_none_call_id_mutation_guard(self):
        # The mutation guard: if the port were built with no call_id (the old
        # hardcoded-None behaviour), the command's call_id is None — so a test
        # asserting a real id would fail. Proves the threading is real, not
        # incidentally-populated.
        port = _port(call_id=None)
        await port.propose(_propose_cmd())

        assert _CapturingService.captured[0].call_id is None
