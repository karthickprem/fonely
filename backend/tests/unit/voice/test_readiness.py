"""Tests for voice readiness gate."""
from fonely.voice.readiness import ReadinessState, VoiceReadinessGate


def test_initial_state():
    gate = VoiceReadinessGate()
    snap = gate.readiness()
    assert snap.state == ReadinessState.NEW
    assert not snap.ready
    assert not gate.is_ready


def test_preload_succeeds():
    gate = VoiceReadinessGate()
    snap = gate.preload()
    assert snap.state == ReadinessState.READY
    assert snap.ready
    assert snap.preload_ms > 0
    assert gate.is_ready


def test_preload_idempotent():
    gate = VoiceReadinessGate()
    s1 = gate.preload()
    s2 = gate.preload()
    assert s1 == s2
    assert gate.is_ready


def test_concurrent_preload():
    import threading

    gate = VoiceReadinessGate()
    results = []

    def worker():
        results.append(gate.preload())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(r.ready for r in results)
    assert len(set(r.preload_ms for r in results)) == 1
