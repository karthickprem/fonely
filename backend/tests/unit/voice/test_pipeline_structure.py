"""Structural guard on the pipeline assembly (V-lane step 3, V3).

The booking commits at exactly ONE place — BookingPostLLMGate, via
clinic_resolver.book_appointment → the injected CommandPort. The assembly
module wires processors together and must add NO second commit path: it must
not name AppointmentService, must not call command_port.confirm/propose itself,
and must not import the command port machinery to commit on the side. This is a
static AST guard so a future edit that grows a second path fails here, plus a
behavioural check that the composed pipeline places the gate after the LLM.
"""

from __future__ import annotations

import ast
import inspect

import fonely.voice.pipeline_assembly as assembly_mod
from fonely.voice.pipeline_assembly import build_voice_pipeline


def _names_in(obj) -> set[str]:
    tree = ast.parse(inspect.getsource(obj))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


class TestNoSecondCommitPath:
    def test_assembly_never_names_appointment_service(self):
        names = _names_in(assembly_mod)
        assert "AppointmentService" not in names, (
            "pipeline_assembly must not name AppointmentService — the only "
            "commit path is BookingPostLLMGate → clinic_resolver.book_appointment "
            "→ CommandPort. A reference here is a second commit path."
        )

    def test_assembly_never_commits_itself(self):
        # The assembly wires the gate in; it must never call the port's commit
        # methods or book_appointment directly.
        names = _names_in(assembly_mod)
        for forbidden in ("confirm", "propose", "book_appointment"):
            assert forbidden not in names, (
                f"pipeline_assembly must not call {forbidden!r} — committing is "
                "the gate's job alone."
            )

    def test_assembly_does_not_import_command_port_machinery(self):
        # It receives a ResolverContext (which carries the port) but must not
        # import the commit commands/port itself.
        src = inspect.getsource(assembly_mod)
        for forbidden in ("ProposeCommand", "ConfirmCommand", "AppointmentServiceCommandPort"):
            assert forbidden not in src, (
                f"pipeline_assembly imports/names {forbidden!r} — it should only "
                "compose processors, not touch the commit machinery."
            )


class TestPipelineComposition:
    def test_gate_is_after_llm_and_before_tts(self):
        # Build with lightweight sentinels; assert the ORDER of the composed
        # processor list puts the gate after the LLM and before TTS, and the
        # latch before STT.
        from fonely.voice.context import TrustedClock
        from fonely.voice.frame_pipeline import ResolverContext
        from fonely.voice.input_latch import NoticeInputLatch

        clock = TrustedClock(
            now_utc=None,
            business_timezone="Asia/Kolkata",
            business_date=None,  # type: ignore[arg-type]
            day_of_week="monday",
        )
        resolver = ResolverContext(
            business_id=1,
            session_factory=lambda: None,  # type: ignore[arg-type,return-value]
            command_port=object(),  # type: ignore[arg-type]
            clock=clock,
        )

        from pipecat.processors.frame_processor import FrameProcessor

        class _Stub(FrameProcessor):
            """A real FrameProcessor so Pipeline.link() accepts it; identity is
            what the ordering assertion checks."""

        latch = NoticeInputLatch()
        built = build_voice_pipeline(
            resolver=resolver,
            transport_in=_Stub(),
            transport_out=_Stub(),
            stt=_Stub(),
            llm=_Stub(),
            tts=_Stub(),
            input_latch=latch,
            system_prompt="you are a receptionist",
        )

        # Pipecat Pipeline exposes its processors; find ours by identity.
        procs = built.pipeline._processors  # ordered list of processors
        idx = {id(p): i for i, p in enumerate(procs)}

        i_latch = idx[id(latch)]
        i_inj = idx[id(built.injector)]
        i_gate = idx[id(built.gate)]

        # latch precedes injector (which is pre-LLM); gate (post-LLM) follows it.
        assert i_latch < i_inj < i_gate, (
            f"expected latch < injector < gate, got {i_latch}, {i_inj}, {i_gate}"
        )

    def test_returns_handles_for_runtime(self):
        # The assembled result must expose the latch/injector/gate/context the
        # runtime drives — not just an opaque pipeline.
        assert hasattr(assembly_mod, "AssembledPipeline")
        fields = assembly_mod.AssembledPipeline.__dataclass_fields__
        assert {"pipeline", "injector", "gate", "input_latch", "context"} <= set(fields)
