"""In-memory test booking engine with real state and idempotency.

NOT a mock — maintains authoritative state, enforces idempotency,
validates target facts, and produces verifiable typed receipts.
Used for provider-free vertical proof; production uses PostgreSQL
AppointmentService through the same CommandPort interface.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, time as dt_time
from typing import Any

from .runtime import CommandPort, CommandResult, ConfirmCommand, ProposeCommand


@dataclass
class Proposal:
    proposal_id: int
    business_id: int
    service_id: int | None
    resource_id: int | None
    target_date: date | None
    target_time: str
    customer_name: str
    idempotency_key: str
    status: str = "pending"
    created_at_ns: int = 0


@dataclass
class Commitment:
    commitment_id: int
    proposal_id: int
    business_id: int
    idempotency_key: str
    facts: dict[str, Any] = field(default_factory=dict)
    committed_at_ns: int = 0


class TestBookingEngine:
    """Stateful in-memory booking engine with idempotency and fact verification.

    Enforces:
    - Proposal idempotency: same key returns same proposal
    - Confirmation requires matching proposal_id and business_id
    - Double confirmation returns existing commitment (idempotent)
    - Receipt is bound to proposal facts, not fabricated
    - Slot conflicts: same resource+date+time cannot be double-booked
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proposals: dict[int, Proposal] = {}
        self._commitments: dict[int, Commitment] = {}
        self._idempotency_index: dict[str, int] = {}
        self._slot_index: set[tuple[int | None, str, str]] = set()
        self._next_proposal_id = 1
        self._next_commitment_id = 1

    async def propose(self, cmd: ProposeCommand) -> CommandResult:
        with self._lock:
            # Idempotency: same key returns existing proposal
            if cmd.idempotency_key and cmd.idempotency_key in self._idempotency_index:
                existing_id = self._idempotency_index[cmd.idempotency_key]
                existing = self._proposals[existing_id]
                if existing.business_id != cmd.business_id:
                    return CommandResult(success=False, error="idempotency_business_mismatch")
                return CommandResult(
                    success=True, operation="create",
                    proposal_id=existing.proposal_id,
                )

            # Slot conflict check
            slot_key = (cmd.resource_id, str(cmd.target_date), cmd.target_time)
            if slot_key in self._slot_index:
                return CommandResult(success=False, error="slot_already_booked")

            # Create proposal
            pid = self._next_proposal_id
            self._next_proposal_id += 1
            proposal = Proposal(
                proposal_id=pid,
                business_id=cmd.business_id,
                service_id=cmd.service_id,
                resource_id=cmd.resource_id,
                target_date=cmd.target_date,
                target_time=cmd.target_time,
                customer_name=cmd.customer_name,
                idempotency_key=cmd.idempotency_key,
                created_at_ns=time.monotonic_ns(),
            )
            self._proposals[pid] = proposal
            if cmd.idempotency_key:
                self._idempotency_index[cmd.idempotency_key] = pid

            return CommandResult(
                success=True, operation="create", proposal_id=pid,
            )

    async def confirm(self, cmd: ConfirmCommand) -> CommandResult:
        with self._lock:
            # Validate proposal exists and matches
            proposal = self._proposals.get(cmd.proposal_id)
            if proposal is None:
                return CommandResult(success=False, error="proposal_not_found")
            if proposal.business_id != cmd.business_id:
                return CommandResult(success=False, error="business_mismatch")

            # Idempotent: already committed returns same
            if proposal.status == "committed":
                existing = next(
                    (c for c in self._commitments.values() if c.proposal_id == cmd.proposal_id),
                    None,
                )
                if existing:
                    return CommandResult(
                        success=True, operation="create",
                        proposal_id=cmd.proposal_id, committed=True,
                        evidence=existing.facts,
                    )

            if proposal.status != "pending":
                return CommandResult(success=False, error=f"proposal_status_{proposal.status}")

            # Reserve slot
            slot_key = (proposal.resource_id, str(proposal.target_date), proposal.target_time)
            if slot_key in self._slot_index:
                proposal.status = "slot_taken"
                return CommandResult(success=False, error="slot_already_booked")
            self._slot_index.add(slot_key)

            # Commit
            cid = self._next_commitment_id
            self._next_commitment_id += 1
            proposal.status = "committed"

            facts = {
                "commitment_id": cid,
                "proposal_id": cmd.proposal_id,
                "business_id": proposal.business_id,
                "service_id": proposal.service_id,
                "resource_id": proposal.resource_id,
                "target_date": str(proposal.target_date),
                "target_time": proposal.target_time,
                "customer_name": proposal.customer_name,
                "idempotency_key": proposal.idempotency_key,
                "confirm_idempotency_key": cmd.idempotency_key,
                "committed_at_ns": time.monotonic_ns(),
            }

            commitment = Commitment(
                commitment_id=cid,
                proposal_id=cmd.proposal_id,
                business_id=proposal.business_id,
                idempotency_key=cmd.idempotency_key,
                facts=facts,
                committed_at_ns=facts["committed_at_ns"],
            )
            self._commitments[cid] = commitment

            return CommandResult(
                success=True, operation="create",
                proposal_id=cmd.proposal_id, committed=True,
                evidence=facts,
            )

    @property
    def proposal_count(self) -> int:
        with self._lock:
            return len(self._proposals)

    @property
    def commitment_count(self) -> int:
        with self._lock:
            return len(self._commitments)

    def get_commitment(self, commitment_id: int) -> Commitment | None:
        with self._lock:
            return self._commitments.get(commitment_id)
