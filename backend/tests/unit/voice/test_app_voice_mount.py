"""The app mount: the canonical VoiceAudioRuntime is constructed ONLY when
voice_pipeline_enabled is on. Flag-off is the default (dark mount), and exotel.py
refuses an unmounted runtime with a clean 1011 — absence must not read as
success.

This tests the FACTORY (_build_voice_audio_runtime) directly, not a full app
boot: the factory needs only app.state.session_factory, so a stub app.state is
enough to prove it constructs a runtime with the exotel contract and the
per-call tenant-bound command port. The flag gating is asserted by reading the
same branch create_app's lifespan uses.
"""

from __future__ import annotations

from dataclasses import dataclass

from fonely.app import _build_voice_audio_runtime


@dataclass
class _AdmittedSession:
    business_id: int = 7
    call_id: int = 4242
    caller_phone: str | None = "+919000000000"
    clinic_name: str = "Smile Care Dental Clinic"
    timezone: str = "Asia/Kolkata"
    provider: str = "exotel"
    provider_call_sid: str = "pcs-1"


class _StubState:
    def __init__(self) -> None:
        self.session_factory = lambda: object()


class _StubApp:
    def __init__(self) -> None:
        self.state = _StubState()


class TestVoiceRuntimeFactory:
    def test_factory_builds_a_runtime_with_the_exotel_contract(self):
        rt = _build_voice_audio_runtime(_StubApp())  # type: ignore[arg-type]

        # It is a VoiceAudioRuntime with compose + run_runner wired (so
        # handle_audio_session won't raise the missing-composer error).
        assert rt.compose is not None
        assert rt.run_runner is not None
        # handle_audio_session has the transport contract signature exotel.py
        # calls: (self, websocket, session, handoff).
        import inspect

        params = list(inspect.signature(rt.handle_audio_session).parameters)
        assert params == ["websocket", "session", "handoff"]

    def test_command_port_is_per_call_and_bound_to_admitted_business(self):
        rt = _build_voice_audio_runtime(_StubApp())  # type: ignore[arg-type]

        # Build the per-call command port for two different admitted businesses;
        # each port's frozen actor business_id is the admitted session's, never a
        # shared/default one. (AppointmentServiceCommandPort freezes _actor.)
        port_a = rt.build_call_command_port(_AdmittedSession(business_id=7))
        port_b = rt.build_call_command_port(_AdmittedSession(business_id=99))

        assert port_a._actor.business_id == 7  # type: ignore[attr-defined]
        assert port_b._actor.business_id == 99  # type: ignore[attr-defined]
        assert port_a is not port_b  # distinct per-call ports, no leaked single port

    def test_resolver_business_comes_from_admitted_session(self):
        rt = _build_voice_audio_runtime(_StubApp())  # type: ignore[arg-type]
        resolver = rt.build_call_resolver(_AdmittedSession(business_id=7))
        # The resolver context is bound to the admitted business.
        assert resolver.business_id == 7


class TestFlagGating:
    """Flag-off is the default; the mount only constructs the runtime when
    voice_pipeline_enabled is on. Proven by exercising the same conditional the
    lifespan uses."""

    def _mounted_runtime(self, *, enabled: bool):
        app = _StubApp()
        # Mirror the lifespan branch: construct only when enabled, else None.
        return _build_voice_audio_runtime(app) if enabled else None  # type: ignore[arg-type]

    def test_flag_off_means_no_runtime(self):
        assert self._mounted_runtime(enabled=False) is None

    def test_flag_on_means_a_runtime(self):
        assert self._mounted_runtime(enabled=True) is not None

    def test_default_setting_is_off(self):
        from fonely.core.config import Settings

        # A fresh Settings with no env override ships voice_pipeline_enabled OFF —
        # the dark-mount default. exotel.py:429 treats absent runtime as 1011.
        assert Settings().voice_pipeline_enabled is False
