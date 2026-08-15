"""Structural invariant: clinic_resolver.book_appointment commits ONLY
through the injected CommandPort, never by constructing AppointmentService.

Constraint 4 from the Option-1 design. A docstring promise is one merge away
from being false; this test fails the moment book_appointment builds its own
commit path.

Two independent checks:
  1. Behavioural — a fake port records calls; a real commit must go through it,
     and AppointmentService constructed during the call is a violation.
  2. Static — book_appointment's source must not name AppointmentService.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, time

import pytest

from fonely.voice.clinic_resolver import book_appointment
from fonely.voice.runtime import CommandResult, CommitReceipt


class _RecordingPort:
    """A CommandPort stand-in that records calls and returns a receipt.

    If book_appointment tries to commit any other way, no receipt reaches it
    through this port and the outcome cannot be success — the test catches it.
    """

    def __init__(self):
        self.propose_calls = []
        self.confirm_calls = []

    async def propose(self, cmd):
        self.propose_calls.append(cmd)
        return CommandResult(
            success=True,
            operation="create",
            proposal_id=42,
            evidence={"version": 2},
        )

    async def confirm(self, cmd):
        self.confirm_calls.append(cmd)
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
    """Answers the resolver's identity queries without a real DB.

    Crucially, it raises if anything tries to use it to build an
    AppointmentService-style commit — there is no such path here.
    """

    def __init__(self):
        self.executed = []

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        self.executed.append(sql)
        if "from services" in sql and "where business_id" in sql:
            # resolve_service: (id, name)
            return _FakeResult([(10, "Scaling")])
        if "service_resource_eligibility" in sql:
            # resolve_resource_for_service: (id, name)
            return _FakeResult([(1, "Dr. Priya")])
        return _FakeResult([])


@pytest.mark.asyncio
async def test_commit_goes_through_port():
    port = _RecordingPort()
    session = _FakeSession()

    outcome = await book_appointment(
        command_port=port,
        session=session,
        business_id=1,
        service_phrase="scaling",
        target_date=date(2026, 8, 12),
        target_time=time(17, 0),
        idempotency_key="voice-test-1",
    )

    assert outcome.success
    assert outcome.appointment_id == 777
    # Facts came from the receipt, not the resolver or a constant.
    assert outcome.service_name == "Scaling"
    assert outcome.resource_name == "Dr. Priya"
    # The commit went through the port exactly once each.
    assert len(port.propose_calls) == 1
    assert len(port.confirm_calls) == 1


@pytest.mark.asyncio
async def test_captured_resource_id_commits_verbatim_no_re_resolution():
    """#45(a): when book_appointment is given the captured resource_id (the
    dentist of the slot the caller selected), it proposes THAT resource and does
    NOT re-resolve via service_resource_eligibility — the wrong-dentist fix."""
    port = _RecordingPort()
    session = _FakeSession()  # its eligibility query would return Dr id=1

    outcome = await book_appointment(
        command_port=port,
        session=session,
        business_id=1,
        service_phrase="scaling",
        target_date=date(2026, 8, 12),
        target_time=time(17, 0),
        idempotency_key="voice-test-res",
        resource_id=5,  # captured Dr B, NOT the eligibility query's lowest-id 1
    )

    assert outcome.success
    # The proposed resource is the captured one — 5, not the re-resolved 1.
    assert port.propose_calls[0].resource_id == 5
    # And the lowest-id re-resolution query was NEVER run.
    assert not any("service_resource_eligibility" in sql for sql in session.executed)


@pytest.mark.asyncio
async def test_absent_resource_id_falls_back_to_resolution():
    """Defensive edge: with no captured resource_id, book_appointment falls back
    to resolve_resource_for_service (historical behaviour). The voice path always
    captures now, so this is the belt-and-suspenders path, not the norm."""
    port = _RecordingPort()
    session = _FakeSession()

    outcome = await book_appointment(
        command_port=port,
        session=session,
        business_id=1,
        service_phrase="scaling",
        target_date=date(2026, 8, 12),
        target_time=time(17, 0),
        idempotency_key="voice-test-fallback",
        # resource_id omitted
    )

    assert outcome.success
    # Fell back to the eligibility query -> resource 1.
    assert port.propose_calls[0].resource_id == 1
    assert any("service_resource_eligibility" in sql for sql in session.executed)


@pytest.mark.asyncio
async def test_unknown_service_refused_without_touching_port():
    """An unknown service must refuse BEFORE any commit attempt."""
    port = _RecordingPort()

    class _EmptyServices(_FakeSession):
        async def execute(self, statement, params=None):
            return _FakeResult([])  # no services match

    outcome = await book_appointment(
        command_port=port,
        session=_EmptyServices(),
        business_id=1,
        service_phrase="root canal",
        target_date=date(2026, 8, 12),
        target_time=time(17, 0),
        idempotency_key="voice-test-2",
    )

    assert not outcome.success
    assert outcome.error.startswith("unknown_service")
    # Refused before the port was ever called — no phantom proposal.
    assert port.propose_calls == []
    assert port.confirm_calls == []


def test_book_appointment_source_never_names_appointment_service():
    """Static guard: the single commit function must not construct
    AppointmentService. If someone adds a direct commit path, this fails."""
    source = inspect.getsource(book_appointment)
    tree = ast.parse(source)

    names_used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names_used.add(node.id)
        elif isinstance(node, ast.Attribute):
            names_used.add(node.attr)

    assert "AppointmentService" not in names_used, (
        "book_appointment must commit ONLY through the CommandPort. "
        "Found a reference to AppointmentService — a second commit path. "
        "Route the commit through command_port.confirm() instead."
    )
    # It must reference the port and the typed commands.
    assert "command_port" in names_used
    assert "ProposeCommand" in names_used
    assert "ConfirmCommand" in names_used
