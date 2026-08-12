"""Pure composition of the voice booking pipeline.

Assembles the pieces that already exist — the provider services
(``build_stt``/``build_llm``/``build_tts``), the deterministic booking
FrameProcessors (``BookingStateInjector`` pre-LLM, ``BookingPostLLMGate``
post-LLM), the LLM context aggregators, and the notice input latch — into ONE
Pipecat ``Pipeline``. It performs no I/O and opens no socket; the caller owns
the transport, the runner, and the run loop.

Frame order (the single canonical arrangement):

    transport_in
      → NoticeInputLatch          (drops caller audio until the notice completes)
      → STT
      → context aggregator .user()
      → BookingStateInjector      (rewrites the LLM context pre-inference)
      → LLM
      → BookingPostLLMGate        (deterministic gates; the SOLE commit route)
      → TTS
      → transport_out
      → context aggregator .assistant()

The gate is the only place a booking commits, and it does so exclusively via
``clinic_resolver.book_appointment`` → the injected ``CommandPort``. This module
never touches the command port, never names ``AppointmentService``, and adds no
second commit path — ``test_pipeline_structure`` enforces that structurally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)

from .frame_pipeline import (
    BookingPostLLMGate,
    BookingStateInjector,
    ResolverContext,
)
from .input_latch import NoticeInputLatch


@dataclass(frozen=True)
class AssembledPipeline:
    """The composed pipeline plus the handles the runtime needs to drive it:
    the injector/gate (per-call mutable state + the commit route), the input
    latch (opened after the notice), and the shared LLM context."""

    pipeline: Pipeline
    injector: BookingStateInjector
    gate: BookingPostLLMGate
    input_latch: NoticeInputLatch
    context: LLMContext


def build_voice_pipeline(
    *,
    resolver: ResolverContext,
    transport_in: Any,
    transport_out: Any,
    stt: Any,
    llm: Any,
    tts: Any,
    input_latch: NoticeInputLatch,
    system_prompt: str,
) -> AssembledPipeline:
    """Compose the booking pipeline. Pure: constructs and wires, runs nothing.

    ``transport_in``/``transport_out``, ``stt``/``llm``/``tts`` are injected so
    tests pass fakes and the runtime passes the real Pipecat transport + the
    provider services from ``providers.py``. ``system_prompt`` seeds the shared
    ``LLMContext``; the injector rewrites it per turn.
    """
    context = LLMContext(messages=[{"role": "system", "content": system_prompt}])
    aggregators = LLMContextAggregatorPair(context)

    injector = BookingStateInjector(resolver)
    gate = BookingPostLLMGate(injector, resolver)

    pipeline = Pipeline(
        [
            transport_in,
            input_latch,
            stt,
            aggregators.user(),
            injector,
            llm,
            gate,
            tts,
            transport_out,
            aggregators.assistant(),
        ]
    )

    return AssembledPipeline(
        pipeline=pipeline,
        injector=injector,
        gate=gate,
        input_latch=input_latch,
        context=context,
    )
