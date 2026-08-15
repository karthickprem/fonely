"""VoiceAudioRuntime dependency discipline (V-lane step 4c).

The injected-instance trap: if the runtime built its own command port, or took a
defaulted one, or rebuilt the resolver per call, a commit-count assertion could
be read off an instance the conversation never used — zero invocations on the
port you hold, because the commit went through a different object, which looks
identical to "never booked". These tests assert the runtime threads the EXACT
injected port and that the release fires exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from fonely.voice.audio_runtime import VoiceAudioRuntime
from fonely.voice.open_order import OpenOutcome, OpenResult


@dataclass
class _FakeAudioSession:
    business_id: int = 1
    call_id: int = 99
    caller_phone: str | None = "+919000000000"
    clinic_name: str = "Smile Dental"
    timezone: str = "Asia/Kolkata"
    provider: str = "exotel"
    provider_call_sid: str = "pcs-1"


class _Port:
    """A distinct command-port object bound to ONE business. The tenant-isolation
    test checks the port a call gets is bound to the ADMITTED session's
    business."""

    def __init__(self, business_id: int) -> None:
        self.business_id = business_id


def _session_bound_factory():
    """A command_port_factory that builds a port bound to the SESSION's business —
    the production contract (business from the admitted session, never a default).
    Returns (factory, built) where `built` records each (session, port)."""
    built: list = []

    def factory(session):
        port = _Port(business_id=session.business_id)
        built.append((session, port))
        return port

    return factory, built


def _runtime(*, resolver_ports: list, releases: list, factory=None) -> VoiceAudioRuntime:
    def resolver_factory(session, command_port):
        # Record WHICH port instance the runtime threaded into the resolver.
        resolver_ports.append(command_port)
        return object()  # a stand-in ResolverContext; identity not needed here

    def release_slot(session):
        releases.append(session)

    if factory is None:
        factory, _ = _session_bound_factory()

    return VoiceAudioRuntime(
        command_port_factory=factory,
        resolver_factory=resolver_factory,
        release_slot=release_slot,
    )


class TestTenantBoundCommandPort:
    """The command port is built PER CALL from the admitted session, bound to
    session.business_id — cross-tenant commit is impossible by construction."""

    def test_port_business_comes_from_the_admitted_session(self):
        factory, built = _session_bound_factory()
        resolver_ports: list = []
        rt = _runtime(resolver_ports=resolver_ports, releases=[], factory=factory)

        rt.build_call_resolver(_FakeAudioSession(business_id=7))

        # The factory was called with the admitted session, and the port it built
        # is bound to THAT session's business — not a default, not model output.
        assert len(built) == 1
        session, port = built[0]
        assert session.business_id == 7
        assert port.business_id == 7
        assert resolver_ports[0] is port  # the per-call port reached the resolver

    def test_two_tenants_get_ports_bound_to_their_own_business(self):
        # Call A (business 7) and call B (business 99) each get a port bound to
        # THEIR admitted business — never the other's.
        factory, built = _session_bound_factory()
        resolver_ports: list = []
        rt = _runtime(resolver_ports=resolver_ports, releases=[], factory=factory)

        rt.build_call_resolver(_FakeAudioSession(business_id=7))
        rt.build_call_resolver(_FakeAudioSession(business_id=99))

        assert built[0][1].business_id == 7
        assert built[1][1].business_id == 99
        # Distinct port instances — no shared/leaked single port across tenants.
        assert built[0][1] is not built[1][1]

    def test_mutation_wrong_source_would_bind_wrong_business(self):
        # Adversarial: a factory that ignored the session and used a CONSTANT
        # business would bind call A's port to the wrong business. Prove the test
        # is session-driven by showing a constant-source factory fails the
        # session-derived assertion.
        def constant_factory(session):
            return _Port(business_id=1)  # BUG: ignores session.business_id

        resolver_ports: list = []
        rt = _runtime(resolver_ports=resolver_ports, releases=[], factory=constant_factory)
        rt.build_call_resolver(_FakeAudioSession(business_id=7))

        # A session-driven port would be business 7; the buggy constant factory
        # produced business 1 — the assertion that catches the wrong binding.
        assert resolver_ports[0].business_id != 7  # buggy factory IS detectable
        assert resolver_ports[0].business_id == 1


class TestReleaseGuardWiring:
    def test_release_guard_fires_slot_release_exactly_once(self):
        releases: list = []
        rt = _runtime(resolver_ports=[], releases=releases)
        session = _FakeAudioSession()

        guard = rt.make_release_guard(session)
        guard.release()
        guard.release()  # e.g. explicit path + outer finally

        assert len(releases) == 1
        assert releases[0] is session
        assert guard.released is True

    def test_release_guard_not_fired_means_no_release(self):
        releases: list = []
        rt = _runtime(resolver_ports=[], releases=releases)
        rt.make_release_guard(_FakeAudioSession())
        # Never called → nothing released (the count is real, not constant).
        assert releases == []

    @pytest.mark.asyncio
    async def test_release_guard_fires_once_even_if_body_raises(self):
        releases: list = []
        rt = _runtime(resolver_ports=[], releases=releases)
        guard = rt.make_release_guard(_FakeAudioSession())

        with pytest.raises(ValueError):
            try:
                raise ValueError("call blew up mid-pipeline")
            finally:
                guard.release()

        assert len(releases) == 1


class TestRunCallOpen:
    """The outer open contract: caller audio (start_conversation) runs ONLY on
    OPENED; a failed open tears down without starting; the admission slot
    releases exactly once on every path. The compliance ordering itself lives in
    run_open_sequence (proven in test_notice_ordering) — this asserts the
    runtime honours its result and never starts the conversation on failure.
    """

    def _rt_with_events(self, events: list, releases: list) -> VoiceAudioRuntime:
        def resolver_factory(session, command_port):
            return object()

        def release_slot(session):
            releases.append(session)
            events.append("release")

        return VoiceAudioRuntime(
            command_port_factory=lambda s: _Port(business_id=s.business_id),
            resolver_factory=resolver_factory,
            release_slot=release_slot,
        )

    @pytest.mark.asyncio
    async def test_opened_starts_conversation_then_releases_once(self):
        events: list = []
        releases: list = []
        rt = self._rt_with_events(events, releases)

        async def open_sequence() -> OpenResult:
            events.append("open")
            return OpenResult(OpenOutcome.OPENED, stt_opened=True, content_digest="d")

        async def start_conversation() -> None:
            events.append("start_conversation")

        async def teardown() -> None:
            events.append("teardown")

        result = await rt.run_call_open(
            _FakeAudioSession(),
            open_sequence=open_sequence,
            start_conversation=start_conversation,
            teardown=teardown,
        )

        assert result.outcome is OpenOutcome.OPENED
        # Conversation started AFTER a successful open; teardown not called; slot
        # released exactly once, last.
        assert events == ["open", "start_conversation", "release"]
        assert len(releases) == 1

    @pytest.mark.asyncio
    async def test_failed_open_never_starts_conversation(self):
        # This is the CEO's hard requirement: no caller audio to STT unless the
        # open sequence opened. start_conversation is what lets audio flow, so it
        # must NOT run when the open failed.
        for outcome in (
            OpenOutcome.NOTICE_PLAYBACK_FAILED,
            OpenOutcome.EVIDENCE_WRITE_FAILED,
        ):
            events: list = []
            releases: list = []
            rt = self._rt_with_events(events, releases)

            async def open_sequence(_o=outcome, _ev=events) -> OpenResult:
                _ev.append("open")
                return OpenResult(_o, stt_opened=False)

            async def start_conversation(_ev=events) -> None:
                _ev.append("start_conversation")

            async def teardown(_ev=events) -> None:
                _ev.append("teardown")

            result = await rt.run_call_open(
                _FakeAudioSession(),
                open_sequence=open_sequence,
                start_conversation=start_conversation,
                teardown=teardown,
            )

            assert result.outcome is outcome
            assert "start_conversation" not in events  # audio never reached STT
            assert events == ["open", "teardown", "release"]
            assert len(releases) == 1

    @pytest.mark.asyncio
    async def test_release_once_even_if_open_raises(self):
        events: list = []
        releases: list = []
        rt = self._rt_with_events(events, releases)

        async def open_sequence() -> OpenResult:
            raise RuntimeError("open blew up")

        async def start_conversation() -> None:
            events.append("start_conversation")

        async def teardown() -> None:
            events.append("teardown")

        with pytest.raises(RuntimeError):
            await rt.run_call_open(
                _FakeAudioSession(),
                open_sequence=open_sequence,
                start_conversation=start_conversation,
                teardown=teardown,
            )

        assert "start_conversation" not in events
        assert len(releases) == 1  # slot still released exactly once


class TestHandleAudioSession:
    """Top-level composition: handle_audio_session composes the call from the
    trusted (websocket, session, handoff) and drives run_call_open. The compose
    and run_runner seams are injected at CONSTRUCTION (so the method signature is
    the clean transport contract), and the proofs are unchanged: the runner runs
    ONLY on OPENED; a failed open never runs the runner; release exactly once.

    This is the exotel.py contract: handle_audio_session(websocket, session,
    handoff) — websocket first, positional, returns None."""

    @dataclass
    class _Handoff:
        start: object = None
        raw_frames: tuple = ()

    def _components(self, *, outcome: OpenOutcome, stt_opened: bool, ran: list) -> object:
        from fonely.voice.audio_runtime import CallComponents

        async def open_sequence() -> OpenResult:
            return OpenResult(outcome, stt_opened=stt_opened, content_digest="d")

        async def teardown() -> None:
            ran.append("teardown")

        return CallComponents(
            input_latch=object(),  # type: ignore[arg-type]
            open_sequence=open_sequence,
            teardown=teardown,
            pipeline_task=object(),
            runner=object(),
        )

    def _rt(
        self,
        *,
        releases: list,
        compose=None,
        run_runner=None,
    ) -> VoiceAudioRuntime:
        return VoiceAudioRuntime(
            command_port_factory=lambda s: _Port(business_id=s.business_id),
            resolver_factory=lambda s, p: object(),
            release_slot=lambda s: releases.append(s),
            compose=compose,
            run_runner=run_runner,
        )

    @pytest.mark.asyncio
    async def test_opened_runs_the_runner_once(self):
        releases: list = []
        ran: list = []
        composed_with: list = []

        def compose(websocket, session, handoff):
            composed_with.append((websocket, session, handoff))
            return self._components(outcome=OpenOutcome.OPENED, stt_opened=True, ran=ran)

        async def run_runner(components) -> None:
            ran.append("run_runner")

        rt = self._rt(releases=releases, compose=compose, run_runner=run_runner)

        ws = object()
        session = _FakeAudioSession()
        handoff = self._Handoff()
        # Called EXACTLY as exotel.py:516 does: (websocket, session, handoff).
        result = await rt.handle_audio_session(ws, session, handoff)

        assert result is None  # transport contract: returns None
        assert ran == ["run_runner"]  # runner ran, teardown not called
        assert len(releases) == 1
        # compose received the websocket + the trusted session + handoff, identity
        # threaded straight through (no re-derivation).
        assert composed_with == [(ws, session, handoff)]

    @pytest.mark.asyncio
    async def test_failed_open_never_runs_the_runner(self):
        releases: list = []
        ran: list = []

        def compose(websocket, session, handoff):
            return self._components(
                outcome=OpenOutcome.EVIDENCE_WRITE_FAILED, stt_opened=False, ran=ran
            )

        async def run_runner(components) -> None:
            ran.append("run_runner")

        rt = self._rt(releases=releases, compose=compose, run_runner=run_runner)

        await rt.handle_audio_session(object(), _FakeAudioSession(), self._Handoff())

        assert "run_runner" not in ran  # runner NEVER ran on a failed open
        assert ran == ["teardown"]
        assert len(releases) == 1

    @pytest.mark.asyncio
    async def test_failing_composer_still_releases_once(self):
        # The constructor-injection move must preserve the ability to inject a
        # FAILING composer and observe the release-once contract still holds.
        releases: list = []

        def compose(websocket, session, handoff):
            raise RuntimeError("composition blew up")

        async def run_runner(components) -> None:
            pass

        rt = self._rt(releases=releases, compose=compose, run_runner=run_runner)

        with pytest.raises(RuntimeError, match="composition blew up"):
            await rt.handle_audio_session(object(), _FakeAudioSession(), self._Handoff())

        assert len(releases) == 1  # slot released exactly once even when compose raises

    @pytest.mark.asyncio
    async def test_missing_compose_raises_loudly(self):
        # A runtime with no composer injected must FAIL LOUD on
        # handle_audio_session, not silently compose nothing (absence must not
        # read as success).
        rt = self._rt(releases=[])
        with pytest.raises(RuntimeError, match="requires compose and run_runner"):
            await rt.handle_audio_session(object(), _FakeAudioSession(), self._Handoff())
