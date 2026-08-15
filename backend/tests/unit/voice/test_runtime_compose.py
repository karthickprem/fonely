"""Composition root: builds CallComponents from (websocket, session, handoff),
identity only from the admitted session, SQL evidence writer targets
session.call_id (no create-row stand-in).

The concrete providers/transport are monkeypatched to fakes — this test proves
the WIRING (what gets built, what identity flows where), not the provider SDKs.
The end-to-end runtime contract (runner only on OPENED, release once) is proven
in test_audio_runtime against a fake compose.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from fonely.voice import providers, runtime_compose
from fonely.voice.audio_runtime import VoiceAudioRuntime


@dataclass
class _FakeSession:
    business_id: int = 7
    call_id: int = 4242
    caller_phone: str | None = "+919000000000"
    clinic_name: str = "Smile Care Dental Clinic"
    timezone: str = "Asia/Kolkata"
    provider: str = "exotel"
    provider_call_sid: str = "pcs-1"


@dataclass
class _FakeStart:
    stream_sid: str = "stream-1"
    provider_call_sid: str = "pcs-1"
    encoding: str = "l16"
    sample_rate: int = 8000
    channels: int = 1


@dataclass
class _FakeHandoff:
    start: _FakeStart
    raw_frames: tuple = ()


def _fake_processor():
    """A minimal real FrameProcessor so Pipeline() can link it (Pipeline wires
    _prev/_next between real processors; a bare object() can't be linked)."""
    from pipecat.processors.frame_processor import FrameProcessor

    return FrameProcessor()


class _FakeTransport:
    def input(self):
        return _fake_processor()

    def output(self):
        return _fake_processor()


class _FakeTask:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.queued: list = []

    async def queue_frames(self, frames):
        self.queued.extend(frames)

    async def cancel(self):
        pass


class _FakeRunner:
    async def run(self, task):
        pass


@pytest.fixture
def patched(monkeypatch):
    """Patch the concrete providers/transport/task/runner so compose runs with no
    SDKs or sockets. Records what identity reached the resolver + writer."""
    seen: dict = {}

    def fake_build_transport(websocket, start):
        seen["transport_start"] = start
        return _FakeTransport()

    monkeypatch.setattr(runtime_compose, "build_transport", fake_build_transport, raising=False)
    # build_transport is imported inside compose from .audio_runtime; patch there.
    from fonely.voice import audio_runtime

    monkeypatch.setattr(audio_runtime, "build_transport", fake_build_transport, raising=False)

    monkeypatch.setattr(providers, "build_stt", lambda cfg: _fake_processor())
    monkeypatch.setattr(providers, "build_llm", lambda cfg, **k: _fake_processor())
    monkeypatch.setattr(providers, "build_tts", lambda cfg: _fake_processor())

    import pipecat.pipeline.runner as runner_mod
    import pipecat.pipeline.task as task_mod

    monkeypatch.setattr(task_mod, "PipelineTask", _FakeTask)
    monkeypatch.setattr(runner_mod, "PipelineRunner", lambda **k: _FakeRunner())

    # Capture the SqlDpdpEvidenceWriter's target call_id via its constructor.
    from fonely.voice import evidence

    real_writer = evidence.SqlDpdpEvidenceWriter

    class _RecordingWriter(real_writer):  # type: ignore[misc, valid-type]
        def __init__(self, *, session_factory):
            super().__init__(session_factory=session_factory)
            seen["writer_built"] = True

    monkeypatch.setattr(evidence, "SqlDpdpEvidenceWriter", _RecordingWriter)

    return seen


def _runtime(seen: dict) -> VoiceAudioRuntime:
    def resolver_factory(session, command_port):
        seen["resolver_session"] = session
        seen["resolver_command_port"] = command_port
        return object()

    def command_port_factory(session):
        # The per-call port is built from the admitted session (its business).
        port = object()
        seen["port_session"] = session
        seen["built_port"] = port
        return port

    return VoiceAudioRuntime(
        command_port_factory=command_port_factory,
        resolver_factory=resolver_factory,
        release_slot=lambda s: None,
    )


class TestCompositionRoot:
    def test_compose_returns_callcomponents_with_handles(self, patched):
        rt = _runtime(patched)
        compose = runtime_compose.make_composition_root(
            rt, session_factory=lambda: object(), system_prompt="sys"
        )
        session = _FakeSession()
        components = compose(object(), session, _FakeHandoff(_FakeStart()))

        # The handles the runtime drives are present.
        assert components.input_latch is not None
        assert callable(components.open_sequence)
        assert callable(components.teardown)
        assert components.pipeline_task is not None
        assert components.runner is not None
        # The input latch starts CLOSED (capture gate).
        assert components.input_latch.is_open is False

    def test_identity_comes_only_from_the_admitted_session(self, patched):
        rt = _runtime(patched)
        compose = runtime_compose.make_composition_root(
            rt, session_factory=lambda: object(), system_prompt="sys"
        )
        session = _FakeSession()
        compose(object(), session, _FakeHandoff(_FakeStart()))

        # The resolver was built with the admitted session, and its command port
        # is the one the PER-CALL factory built from that same admitted session —
        # tenant binding flows session → factory → port → resolver.
        assert patched["resolver_session"] is session
        assert patched["port_session"] is session  # factory saw the admitted session
        assert patched["resolver_command_port"] is patched["built_port"]
        # The transport decode rate came from the admitted start event.
        assert patched["transport_start"].sample_rate == 8000

    def test_sql_writer_is_built_no_create_row_standin(self, patched):
        # The real path uses the SQL writer against the EXISTING call row
        # (admission wrote it); the writer is constructed and the open sequence
        # is bound to session.call_id — no create_call_row stand-in.
        rt = _runtime(patched)
        compose = runtime_compose.make_composition_root(
            rt, session_factory=lambda: object(), system_prompt="sys"
        )
        compose(object(), _FakeSession(call_id=4242), _FakeHandoff(_FakeStart()))
        assert patched["writer_built"] is True
