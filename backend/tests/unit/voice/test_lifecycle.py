"""Tests for voice session supervisor state machine."""

import asyncio
import warnings

import pytest

from fonely.voice.config import SessionLimits, SessionState, VoiceSessionConfig
from fonely.voice.lifecycle import VoiceSessionSupervisor


def _config(**kw):
    defaults = {"session_id": "test-1", "business_id": 1}
    defaults.update(kw)
    return VoiceSessionConfig(**defaults)


class TestStateTransitions:
    def test_initial_state(self):
        sup = VoiceSessionSupervisor(_config())
        assert sup.state == SessionState.CREATED

    def test_valid_forward_path(self):
        sup = VoiceSessionSupervisor(_config())
        assert sup.transition(SessionState.SIGNALING)
        assert sup.state == SessionState.SIGNALING
        assert sup.transition(SessionState.CONNECTING)
        assert sup.state == SessionState.CONNECTING
        assert sup.transition(SessionState.ACTIVE)
        assert sup.state == SessionState.ACTIVE

    def test_invalid_transition_rejected(self):
        sup = VoiceSessionSupervisor(_config())
        assert not sup.transition(SessionState.ACTIVE)
        assert sup.state == SessionState.CREATED

    def test_fail_from_any_state(self):
        for state in [
            SessionState.CREATED,
            SessionState.SIGNALING,
            SessionState.CONNECTING,
            SessionState.ACTIVE,
            SessionState.RECONNECTING,
            SessionState.DRAINING,
        ]:
            sup = VoiceSessionSupervisor(_config())
            sup._state = state
            assert sup.transition(SessionState.FAILED)
            assert sup.state == SessionState.FAILED

    def test_terminal_states_no_transitions(self):
        for terminal in [SessionState.CLOSED, SessionState.FAILED]:
            sup = VoiceSessionSupervisor(_config())
            sup._state = terminal
            assert not sup.transition(SessionState.ACTIVE)

    def test_reconnect_cycle(self):
        sup = VoiceSessionSupervisor(_config())
        sup._state = SessionState.ACTIVE
        assert sup.transition(SessionState.RECONNECTING)
        assert sup.transition(SessionState.ACTIVE)


class TestClose:
    @pytest.mark.asyncio
    async def test_normal_close(self):
        sup = VoiceSessionSupervisor(_config())
        sup._state = SessionState.ACTIVE
        summary = await sup.close("normal")
        assert sup.state == SessionState.CLOSED
        assert summary["close_reason"] == "normal"
        assert "duration_ms" in summary

    @pytest.mark.asyncio
    async def test_error_close(self):
        sup = VoiceSessionSupervisor(_config())
        sup._state = SessionState.ACTIVE
        summary = await sup.close("provider_error")
        assert sup.state == SessionState.FAILED
        assert summary["close_reason"] == "provider_error"

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        sup = VoiceSessionSupervisor(_config())
        sup._state = SessionState.ACTIVE
        s1 = await sup.close("normal")
        s2 = await sup.close("second_close")
        assert s1["close_reason"] == "normal"
        assert s2 == sup.telemetry.usage_summary()

    @pytest.mark.asyncio
    async def test_telemetry_emitted_on_create_and_close(self):
        sup = VoiceSessionSupervisor(_config())
        sup._state = SessionState.ACTIVE
        await sup.close("normal")
        events = sup.telemetry.drain()
        names = [e.name for e in events]
        assert "session_created" in names
        assert "session_closed" in names
        assert "session_telemetry_closed" in names


class TestTimerLoopSafety:
    """transition() is synchronous and must be a clean, valid state change even
    when called outside any event loop. The duration/reconnect timers are
    background coroutines that require a RUNNING loop; when none exists the
    transition still succeeds but arms no timer — and crucially constructs no
    coroutine, so there is no RuntimeError and no unawaited-coroutine warning.
    Regression for the py3.14 failure where ensure_future built the coroutine
    before failing on the missing loop.
    """

    def test_transition_to_active_no_loop_arms_no_timer_no_warning(self):
        sup = VoiceSessionSupervisor(_config())
        sup._state = SessionState.CONNECTING
        with warnings.catch_warnings():
            # Any "coroutine was never awaited" RuntimeWarning fails the test.
            warnings.simplefilter("error", RuntimeWarning)
            assert sup.transition(SessionState.ACTIVE) is True
        assert sup.state == SessionState.ACTIVE
        assert sup._duration_timer is None  # no running loop → no timer armed

    def test_reconnect_transition_no_loop_no_warning(self):
        sup = VoiceSessionSupervisor(_config())
        sup._state = SessionState.ACTIVE
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            assert sup.transition(SessionState.RECONNECTING) is True
        assert sup.state == SessionState.RECONNECTING
        assert sup._reconnect_timer is None

    @pytest.mark.asyncio
    async def test_active_arms_duration_timer_and_close_cancels(self):
        # Long limit so the timer stays pending; we assert arm + clean cancel.
        cfg = _config(limits=SessionLimits(max_duration_seconds=3600))
        sup = VoiceSessionSupervisor(cfg)
        sup._state = SessionState.CONNECTING
        assert sup.transition(SessionState.ACTIVE) is True
        assert sup._duration_timer is not None
        assert not sup._duration_timer.done()
        await sup.close("normal")
        # close() cancels the timer; give the loop a tick to process it.
        await asyncio.sleep(0)
        assert sup._duration_timer.cancelled()

    @pytest.mark.asyncio
    async def test_reconnect_arms_timer_then_active_cancels_it(self):
        cfg = _config(limits=SessionLimits(reconnect_grace_seconds=3600))
        sup = VoiceSessionSupervisor(cfg)
        sup._state = SessionState.ACTIVE
        assert sup.transition(SessionState.RECONNECTING) is True
        reconnect_timer = sup._reconnect_timer
        assert reconnect_timer is not None
        assert not reconnect_timer.done()
        # Returning to ACTIVE must cancel the reconnect-grace timer and clear it.
        assert sup.transition(SessionState.ACTIVE) is True
        assert sup._reconnect_timer is None
        await asyncio.sleep(0)
        assert reconnect_timer.cancelled()
        await sup.close("normal")


class TestProperties:
    def test_session_id(self):
        sup = VoiceSessionSupervisor(_config(session_id="abc"))
        assert sup.session_id == "abc"

    def test_clock_available(self):
        sup = VoiceSessionSupervisor(_config())
        token = sup.clock.current()
        assert token.session_id == "test-1"

    def test_default_validator_is_stub(self):
        from fonely.voice.validator_port import FailClosedValidatorStub

        sup = VoiceSessionSupervisor(_config())
        assert isinstance(sup.validator, FailClosedValidatorStub)
