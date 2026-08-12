"""Provider-free load and soak scaffolding.

Tests concurrent session creation, state transitions, turn budgets,
resource cleanup, and memory stability without live providers.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from fonely.voice.config import SessionState, VoiceSessionConfig
from fonely.voice.diagnostics import SessionInfo, SessionRegistry
from fonely.voice.dialogue import DialogueState
from fonely.voice.generation import GenerationClock
from fonely.voice.lifecycle import VoiceSessionSupervisor
from fonely.voice.telemetry import VoiceTelemetryExporter


def _config(sid: str) -> VoiceSessionConfig:
    return VoiceSessionConfig(session_id=sid, business_id=1)


class TestConcurrentSessions:
    def test_10_sessions_register_and_close(self):
        registry = SessionRegistry(max_sessions=20)
        supervisors = []

        for i in range(10):
            cfg = _config(f"load-{i}")
            sup = VoiceSessionSupervisor(cfg)
            info = SessionInfo(
                cfg.session_id,
                SessionState.CREATED,
                cfg.business_id,
                time.monotonic_ns(),
            )
            assert registry.register(info)
            sup.transition(SessionState.SIGNALING)
            sup.transition(SessionState.CONNECTING)
            sup.transition(SessionState.ACTIVE)
            registry.update_state(cfg.session_id, SessionState.ACTIVE)
            supervisors.append(sup)

        assert registry.active_count() == 10

        loop = asyncio.new_event_loop()
        for sup in supervisors:
            summary = loop.run_until_complete(sup.close("normal"))
            registry.unregister(sup.session_id)
            assert summary["final_state"] == "closed"
        loop.close()

        assert registry.active_count() == 0
        diag = registry.diagnostics()
        assert diag["total_created"] == 10
        assert diag["total_closed"] == 10

    def test_concurrent_generation_clocks(self):
        clocks = [GenerationClock(f"sess-{i}") for i in range(10)]
        results = []

        def worker(clock):
            for _ in range(100):
                clock.next_turn()
                clock.advance_generation()
            results.append(clock.turn_count)

        threads = [threading.Thread(target=worker, args=(c,)) for c in clocks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert all(r == 100 for r in results)

    def test_concurrent_telemetry_emission(self):
        tel = VoiceTelemetryExporter("soak-1", max_size=5000)
        errors = []

        def worker(tid):
            try:
                for i in range(500):
                    tel.emit(f"event_{tid}_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0
        assert tel._total_emitted == 5000
        summary = tel.close()
        assert summary["total_emitted"] == 5001  # +1 for close event


class TestTurnBudgetUnderLoad:
    def test_many_sessions_respect_budget(self):
        for _i in range(20):
            ds = DialogueState(max_turns=5)
            for turn in range(6):
                ds.record_turn(f"response {turn}", asked_field="date" if turn % 2 == 0 else "time")
            assert ds.is_over_budget()


class TestResourceCleanup:
    @pytest.mark.asyncio
    async def test_supervisor_close_releases_timers(self):
        sup = VoiceSessionSupervisor(_config("cleanup-1"))
        sup.transition(SessionState.SIGNALING)
        sup.transition(SessionState.CONNECTING)
        sup.transition(SessionState.ACTIVE)
        summary = await sup.close("normal")
        assert summary["final_state"] == "closed"
        if sup._duration_timer is not None:
            await asyncio.sleep(0.01)
            assert sup._duration_timer.done() or sup._duration_timer.cancelled()

    @pytest.mark.asyncio
    async def test_failed_session_cleanup(self):
        sup = VoiceSessionSupervisor(_config("cleanup-2"))
        sup.transition(SessionState.SIGNALING)
        sup.transition(SessionState.FAILED)
        summary = await sup.close("provider_error")
        assert summary["final_state"] == "failed"


class TestMemoryStability:
    def test_telemetry_bounded_under_pressure(self):
        tel = VoiceTelemetryExporter("mem-1", max_size=100)
        for i in range(10000):
            tel.emit(f"pressure_{i}", data={"payload": "x" * 100})
        assert len(tel.drain()) <= 100
        assert tel._dropped > 0
