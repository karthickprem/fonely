"""Tests for operator diagnostics and session registry."""
import time

from fonely.voice.config import SessionState
from fonely.voice.diagnostics import SessionInfo, SessionRegistry


def test_register_and_diagnostics():
    reg = SessionRegistry(max_sessions=5)
    info = SessionInfo("s1", SessionState.ACTIVE, 1, time.monotonic_ns(), 3)
    assert reg.register(info)
    assert reg.active_count() == 1
    diag = reg.diagnostics()
    assert diag["active_sessions"] == 1
    assert diag["total_created"] == 1
    assert diag["sessions"][0]["session_id"] == "s1"
    assert diag["sessions"][0]["turn_count"] == 3


def test_register_capacity_limit():
    reg = SessionRegistry(max_sessions=2)
    reg.register(SessionInfo("s1", SessionState.ACTIVE, 1, time.monotonic_ns()))
    reg.register(SessionInfo("s2", SessionState.ACTIVE, 1, time.monotonic_ns()))
    assert not reg.register(SessionInfo("s3", SessionState.ACTIVE, 1, time.monotonic_ns()))
    assert reg.active_count() == 2


def test_unregister_normal():
    reg = SessionRegistry()
    reg.register(SessionInfo("s1", SessionState.ACTIVE, 1, time.monotonic_ns()))
    reg.unregister("s1")
    assert reg.active_count() == 0
    assert reg.diagnostics()["total_closed"] == 1


def test_unregister_failed():
    reg = SessionRegistry()
    reg.register(SessionInfo("s1", SessionState.ACTIVE, 1, time.monotonic_ns()))
    reg.unregister("s1", failed=True)
    assert reg.diagnostics()["total_failed"] == 1


def test_update_state():
    reg = SessionRegistry()
    reg.register(SessionInfo("s1", SessionState.CONNECTING, 1, time.monotonic_ns()))
    reg.update_state("s1", SessionState.ACTIVE, turn_count=5)
    diag = reg.diagnostics()
    assert diag["sessions"][0]["state"] == SessionState.ACTIVE
    assert diag["sessions"][0]["turn_count"] == 5


def test_credentials_in_diagnostics():
    reg = SessionRegistry()
    diag = reg.diagnostics()
    assert "credentials" in diag
    assert "credentials_ready" in diag
    assert isinstance(diag["credentials"], dict)
