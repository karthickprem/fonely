"""#43 P3: the voice claim-loop logic, proven WITHOUT a database.

The DB-integrated path (real markers, real CAS, cross-process) is the separate PG
+ two-process E2E. These unit tests pin the loop's control logic against fakes:
the local-registry claim filter, reconcile-before-claim ordering, claim-CAS
exactly-once (winner speaks, loser silent), and the digest-validating read.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from fonely.domain.pending_actions.payloads import (
    AwaitingOwnerReplyData,
    build_awaiting_owner_reply_payload,
)
from fonely.domain.pending_actions.snapshots import awaiting_owner_reply_payload_digest
from fonely.models.enums import PendingActionStatus
from fonely.voice import owner_reply_claim_loop as loop


@dataclass
class _FakeMarker:
    id: int = 1
    business_id: int = 1
    call_id: int = 10
    version: int = 1
    status: str = PendingActionStatus.RESUME_REQUESTED.value
    proposed_payload: dict[str, Any] = field(default_factory=dict)
    payload_digest: str = ""

    @classmethod
    def with_answer(cls, answer: str, **kw: Any) -> _FakeMarker:
        payload = build_awaiting_owner_reply_payload()
        data = dict(payload["data"])  # type: ignore[arg-type]
        data["answer_text"] = answer
        payload["data"] = data
        digest = awaiting_owner_reply_payload_digest(payload)
        return cls(proposed_payload=payload, payload_digest=digest, **kw)


class _FakeRegistry:
    """Records local keys and captures what got spoken via resume()."""

    def __init__(self, keys: list[tuple[int, int]]) -> None:
        self._keys = keys
        self.spoken: list[tuple[int, int, str]] = []
        self.resume_returns = True

    async def local_keys(self) -> list[tuple[int, int]]:
        return list(self._keys)

    async def resume(self, business_id: int, call_id: int, text: str) -> bool:
        self.spoken.append((business_id, call_id, text))
        return self.resume_returns


class _FakeSession:
    def __init__(self, marker: _FakeMarker | None, *, cas_wins: bool = True) -> None:
        self._marker = marker
        self._cas_wins = cas_wins
        self.committed = False
        self.rolled_back = False

    async def execute(self, _stmt: Any) -> Any:
        return _ScalarResult(self._marker)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


def _session_factory(session: _FakeSession):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


@pytest.fixture(autouse=True)
def _patch_repo(monkeypatch):
    """Patch PendingActionRepository.conditional_update to a fake CAS whose win/
    lose outcome the test controls, without a DB."""

    class _FakeRepo:
        cas_result: Any = object()  # non-None = win

        def __init__(self, session: Any) -> None:
            self._session = session

        async def conditional_update(self, **_: Any) -> Any:
            return type(self).cas_result

    monkeypatch.setattr("fonely.repositories.pending_actions.PendingActionRepository", _FakeRepo)
    return _FakeRepo


async def _noop_reconcile(_session, _business_id, data: AwaitingOwnerReplyData) -> str:
    return f"reconciled:{data.answer_text}"


class TestLocalKeyFilter:
    @pytest.mark.asyncio
    async def test_no_local_keys_does_nothing(self):
        reg = _FakeRegistry(keys=[])
        session = _FakeSession(marker=None)
        n = await loop._claim_tick(
            resume_registry=reg,
            session_factory=_session_factory(session),
            reconcile_answer=_noop_reconcile,
        )
        assert n == 0
        assert reg.spoken == []


class TestClaimAndResume:
    @pytest.mark.asyncio
    async def test_winner_reconciles_then_speaks(self, _patch_repo):
        _patch_repo.cas_result = object()  # CAS wins
        reg = _FakeRegistry(keys=[(1, 10)])
        marker = _FakeMarker.with_answer("6 to 10 pm")
        session = _FakeSession(marker=marker)
        n = await loop._claim_tick(
            resume_registry=reg,
            session_factory=_session_factory(session),
            reconcile_answer=_noop_reconcile,
        )
        assert n == 1
        # Reconciled text (NOT the owner's verbatim words) was spoken into the call.
        assert reg.spoken == [(1, 10, "reconciled:6 to 10 pm")]
        assert session.committed  # marker durably RESUMED before speech

    @pytest.mark.asyncio
    async def test_lost_cas_race_speaks_nothing(self, _patch_repo):
        _patch_repo.cas_result = None  # CAS lost (another claimer / sweep won)
        reg = _FakeRegistry(keys=[(1, 10)])
        marker = _FakeMarker.with_answer("6 to 10 pm")
        session = _FakeSession(marker=marker)
        n = await loop._claim_tick(
            resume_registry=reg,
            session_factory=_session_factory(session),
            reconcile_answer=_noop_reconcile,
        )
        assert n == 0
        assert reg.spoken == []  # a losing claimer never speaks
        assert session.rolled_back

    @pytest.mark.asyncio
    async def test_no_marker_for_held_call_is_noop(self, _patch_repo):
        reg = _FakeRegistry(keys=[(1, 10)])
        session = _FakeSession(marker=None)  # nothing RESUME_REQUESTED yet
        n = await loop._claim_tick(
            resume_registry=reg,
            session_factory=_session_factory(session),
            reconcile_answer=_noop_reconcile,
        )
        assert n == 0
        assert reg.spoken == []


class TestDigestValidation:
    @pytest.mark.asyncio
    async def test_digest_mismatch_raises_not_speaks(self, _patch_repo):
        # A payload whose digest drifted (P2 mutated without recompute — the bug)
        # must fail loud, NEVER speak a stale/unverified answer.
        reg = _FakeRegistry(keys=[(1, 10)])
        marker = _FakeMarker.with_answer("6 to 10 pm")
        marker.payload_digest = "deadbeef" * 8  # wrong digest
        session = _FakeSession(marker=marker)
        with pytest.raises(ValueError, match="digest mismatch"):
            await loop._claim_and_resume(
                resume_registry=reg,
                session_factory=_session_factory(session),
                reconcile_answer=_noop_reconcile,
                business_id=1,
                call_id=10,
            )
        assert reg.spoken == []


class TestReconcileBeforeClaim:
    @pytest.mark.asyncio
    async def test_reconcile_failure_does_not_claim_marker(self, _patch_repo):
        # If reconciliation raises, the marker must stay RESUME_REQUESTED (not
        # burned) so it's retried next tick. The claim CAS must not have run.
        _patch_repo.cas_result = object()
        reg = _FakeRegistry(keys=[(1, 10)])
        marker = _FakeMarker.with_answer("6 to 10 pm")
        session = _FakeSession(marker=marker)

        async def _boom(_s, _b, _d):
            raise RuntimeError("engine down")

        with pytest.raises(RuntimeError, match="engine down"):
            await loop._claim_and_resume(
                resume_registry=reg,
                session_factory=_session_factory(session),
                reconcile_answer=_boom,
                business_id=1,
                call_id=10,
            )
        assert reg.spoken == []
        assert not session.committed  # marker not flipped to RESUMED
