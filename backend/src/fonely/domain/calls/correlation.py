"""Call correlation — provider-independent callback verification.

Correlates callbacks against gateway-admitted call-session records.
Three outcomes: matched, pending (quarantine), conflict (dead-letter).

Quarantine rows are written ONLY after gateway authentication.
Correlation does not replace semantic dedup or transition validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class CorrelationOutcome(StrEnum):
    MATCHED = "matched"
    PENDING = "pending"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class CorrelationRecord:
    """Server-created admitted call-session record."""

    provider: str
    provider_account_id: str
    provider_call_id: str
    called_number: str
    business_id: int
    direction: str | None


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    outcome: CorrelationOutcome
    record: CorrelationRecord | None


class CallCorrelationStore(Protocol):
    """Interface for correlation record storage."""

    async def register_admitted_call(self, record: CorrelationRecord) -> None:
        """Register a call-session record from an authenticated start."""
        ...

    async def correlate(
        self,
        provider: str,
        provider_account_id: str,
        provider_call_id: str,
        called_number: str,
        business_id: int,
        direction: str | None,
    ) -> CorrelationResult:
        """Check a callback against admitted records.

        Returns:
        - MATCHED if all fields agree within validity window
        - PENDING if no record exists yet
        - CONFLICT if fields disagree (wrong business, number, etc)
        """
        ...

    async def reconcile_pending(
        self,
        provider: str,
        provider_call_id: str,
    ) -> list[int]:
        """When a session record arrives, reconcile pending quarantine
        events for this call. Returns event IDs now eligible."""
        ...


class InMemoryCorrelationStore:
    """Test implementation of CallCorrelationStore."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], CorrelationRecord] = {}

    async def register_admitted_call(self, record: CorrelationRecord) -> None:
        key = (record.provider, record.provider_call_id)
        self._records[key] = record

    async def correlate(
        self,
        provider: str,
        provider_account_id: str,
        provider_call_id: str,
        called_number: str,
        business_id: int,
        direction: str | None,
    ) -> CorrelationResult:
        key = (provider, provider_call_id)
        record = self._records.get(key)

        if record is None:
            return CorrelationResult(outcome=CorrelationOutcome.PENDING, record=None)

        if (
            record.provider_account_id != provider_account_id
            or record.called_number != called_number
            or record.business_id != business_id
        ):
            return CorrelationResult(outcome=CorrelationOutcome.CONFLICT, record=record)

        return CorrelationResult(outcome=CorrelationOutcome.MATCHED, record=record)

    async def reconcile_pending(
        self,
        provider: str,
        provider_call_id: str,
    ) -> list[int]:
        return []
