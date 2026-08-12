"""Exactly-once admission release across every terminal path (V-lane, T5).

The guarantee is asserted by COUNT, not by "the call ended" — a leaked release
and a double release both end the call looking normal, so the observable that
distinguishes them is how many times the underlying release ran. It must be
exactly one on normal completion, on an exception, on cancellation, and when the
guard is called repeatedly (belt-and-suspenders finally + explicit path calls).
"""

from __future__ import annotations

import asyncio

import pytest

from fonely.voice.admission import AdmissionController
from fonely.voice.call_teardown import OnceRelease


class _Counter:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


class TestOnceRelease:
    def test_single_release(self):
        c = _Counter()
        r = OnceRelease(c)
        r.release()
        assert c.count == 1
        assert r.released is True

    def test_repeated_calls_release_once(self):
        # The common shape: an explicit release on the normal path AND the outer
        # finally both call it. Underlying release must run exactly once.
        c = _Counter()
        r = OnceRelease(c)
        r.release()
        r.release()
        r.release()
        assert c.count == 1

    @pytest.mark.asyncio
    async def test_release_once_when_body_raises(self):
        c = _Counter()
        r = OnceRelease(c)
        with pytest.raises(RuntimeError):
            try:
                raise RuntimeError("pipeline blew up")
            finally:
                r.release()
        assert c.count == 1

    @pytest.mark.asyncio
    async def test_release_once_on_cancellation(self):
        c = _Counter()
        r = OnceRelease(c)

        async def body() -> None:
            try:
                await asyncio.sleep(3600)
            finally:
                r.release()

        task = asyncio.create_task(body())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert c.count == 1

    def test_never_called_means_never_released(self):
        # Sanity: the guard does not release on its own — the runtime must call
        # it. (Proves the count is real, not a constant.)
        c = _Counter()
        OnceRelease(c)
        assert c.count == 0


class TestAdmissionReleaseIntegration:
    def test_admit_then_once_release_returns_slot_exactly_once(self):
        admission = AdmissionController(max_per_tenant=2, max_global=5)
        decision = admission.try_admit("clinic-1")
        assert decision.admitted
        assert admission.stats()["global_active"] == 1

        r = OnceRelease(lambda: admission.release("clinic-1"))
        # Called from multiple terminal paths...
        r.release()
        r.release()
        # ...the slot returns to baseline exactly once (not driven negative).
        assert admission.stats()["global_active"] == 0

    def test_double_release_would_undercount_without_the_guard(self):
        # Demonstrates WHY the guard matters: two raw releases of one slot, with
        # a second tenant holding a slot, would under-count global. The guard
        # prevents the second release from ever running.
        admission = AdmissionController(max_per_tenant=2, max_global=5)
        admission.try_admit("clinic-1")
        admission.try_admit("clinic-2")
        assert admission.stats()["global_active"] == 2

        r = OnceRelease(lambda: admission.release("clinic-1"))
        r.release()
        r.release()  # no-op
        # clinic-2's slot is untouched; global is exactly 1, not 0.
        assert admission.stats()["global_active"] == 1
