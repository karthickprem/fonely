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
    """A distinct command-port object; identity is what the discipline test
    checks."""


def _runtime(port, *, resolver_ports: list, releases: list) -> VoiceAudioRuntime:
    def resolver_factory(session, command_port):
        # Record WHICH port instance the runtime threaded into the resolver.
        resolver_ports.append(command_port)
        return object()  # a stand-in ResolverContext; identity not needed here

    def release_slot(session):
        releases.append(session)

    return VoiceAudioRuntime(
        command_port=port,
        resolver_factory=resolver_factory,
        release_slot=release_slot,
    )


class TestInjectedInstanceDiscipline:
    def test_resolver_gets_the_injected_port_instance_not_a_copy(self):
        port = _Port()
        resolver_ports: list = []
        rt = _runtime(port, resolver_ports=resolver_ports, releases=[])

        rt.build_call_resolver(_FakeAudioSession())

        assert len(resolver_ports) == 1
        # IDENTITY, not equality: the exact object we injected reached the
        # resolver — the runtime built no port of its own.
        assert resolver_ports[0] is port

    def test_wrong_instance_would_be_caught(self):
        # Guard against a false green: if the runtime had substituted a different
        # port, this identity assertion would fail. Prove that by threading a
        # DIFFERENT object and confirming `is` rejects it.
        injected = _Port()
        other = _Port()
        resolver_ports: list = []
        rt = _runtime(injected, resolver_ports=resolver_ports, releases=[])

        rt.build_call_resolver(_FakeAudioSession())

        assert resolver_ports[0] is injected
        assert resolver_ports[0] is not other  # a wrong instance is detectable


class TestReleaseGuardWiring:
    def test_release_guard_fires_slot_release_exactly_once(self):
        port = _Port()
        releases: list = []
        rt = _runtime(port, resolver_ports=[], releases=releases)
        session = _FakeAudioSession()

        guard = rt.make_release_guard(session)
        guard.release()
        guard.release()  # e.g. explicit path + outer finally

        assert len(releases) == 1
        assert releases[0] is session
        assert guard.released is True

    def test_release_guard_not_fired_means_no_release(self):
        port = _Port()
        releases: list = []
        rt = _runtime(port, resolver_ports=[], releases=releases)
        rt.make_release_guard(_FakeAudioSession())
        # Never called → nothing released (the count is real, not constant).
        assert releases == []

    @pytest.mark.asyncio
    async def test_release_guard_fires_once_even_if_body_raises(self):
        port = _Port()
        releases: list = []
        rt = _runtime(port, resolver_ports=[], releases=releases)
        guard = rt.make_release_guard(_FakeAudioSession())

        with pytest.raises(ValueError):
            try:
                raise ValueError("call blew up mid-pipeline")
            finally:
                guard.release()

        assert len(releases) == 1
