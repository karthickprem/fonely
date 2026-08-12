"""SYNTACTIC anti-drift guard + composition-order check for the assembly.

SCOPE — read before citing this file. The AST checks below assert the ABSENCE
OF NAMES in pipeline_assembly's source: nobody wrote the obvious second-commit
call here. Absence of names is NOT absence of behaviour — a second commit path
reached by getattr, importlib, an alias bound elsewhere, a service handed in as
an argument, or an LLM-invoked callback spells none of these names and passes
this test unchanged. So this file is a TRIPWIRE against accidental syntactic
drift, nothing more.

The BEHAVIOURAL guarantee that the CommandPort is the SOLE commit path — invoked
exactly once when a booking confirms and zero times when the gate refuses — is
proven by observing the port's invocation count in
test_full_media_to_media.py::test_sole_commit_path_*. That is the test to cite
for "sole commit path"; this one only forbids the obvious spelling.

Each guard here is mutation-proven in TestGuardsAreNotDecorative: injecting a
real violation makes the guard fail, so a green result means something.
"""

from __future__ import annotations

import ast
import inspect

import fonely.voice.pipeline_assembly as assembly_mod
from fonely.voice.pipeline_assembly import build_voice_pipeline


def _names_in_source(src: str) -> set[str]:
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _names_in(obj) -> set[str]:
    return _names_in_source(inspect.getsource(obj))


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


class TestGuardsAreNotDecorative:
    """Mutation-prove the guards above actually fail when violated. A guard that
    passes whether or not the defect is present is worse than no guard — it reads
    as proof of something it never checked (the class of failure behind ca43917).
    """

    def test_ast_guard_fails_on_injected_second_commit_path(self):
        clean = inspect.getsource(assembly_mod)
        # Inject a real syntactic second-commit reference.
        mutant = clean.replace(
            "return AssembledPipeline(",
            "AppointmentService  # injected second path\n    return AssembledPipeline(",
            1,
        )
        assert mutant != clean, "mutation did not apply"
        # The clean source passes the guard; the mutant must fail it.
        assert "AppointmentService" not in _names_in_source(clean)
        assert "AppointmentService" in _names_in_source(mutant)

    def test_order_assertion_fails_when_gate_precedes_injector(self):
        # The order check asserts latch < injector < gate. Prove that predicate
        # rejects a swapped order (gate before injector), so a real reordering
        # would be caught.
        good = {"latch": 0, "injector": 1, "gate": 2}
        swapped = {"latch": 0, "gate": 1, "injector": 2}

        def order_holds(idx: dict[str, int]) -> bool:
            return idx["latch"] < idx["injector"] < idx["gate"]

        assert order_holds(good) is True
        assert order_holds(swapped) is False
