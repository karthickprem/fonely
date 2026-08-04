import asyncio
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

LAB = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB))

from pipecat.frames.frames import InterruptionFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import run_test

from delegation import GenerationOutputGate
from live_poc import (
    DelegateStatus,
    GenerationClock,
    RealtimePOCCoordinator,
    TranscriptStage,
    delayed_lookup,
)
from voice_eval.contracts import load_json_schema


def test_generation_clock_is_session_local_and_monotonic():
    first = GenerationClock("a")
    second = GenerationClock("b")
    assert first.next_turn().turn_id == 1
    assert first.advance_generation().generation_id == 1
    assert second.current.turn_id == 0 and second.current.generation_id == 0


def test_speculative_final_and_authoritative_are_distinct():
    coordinator = RealtimePOCCoordinator("session")
    partial = coordinator.record_transcript("நாளைக்கு", finalized=False)
    final = coordinator.record_transcript("நாளைக்கு ஐந்து மணி", finalized=True)
    assert partial.stage == TranscriptStage.SPECULATIVE and not partial.authoritative
    assert final.stage == TranscriptStage.FINAL and not final.authoritative
    assert coordinator.mark_authoritative(final).authoritative
    with pytest.raises(ValueError):
        coordinator.mark_authoritative(partial)


def test_late_delegate_result_is_stale_suppressed():
    async def run():
        events = []
        coordinator = RealtimePOCCoordinator("session", event_sink=events.append)
        coordinator.record_transcript("final", finalized=True)
        task = asyncio.create_task(
            coordinator.start_delegate(
                lambda: delayed_lookup(
                    delay_ms=20,
                    result={"value": "must not escape"},
                    cooperative_cancel=False,
                ),
                timeout_ms=100,
            )
        )
        await asyncio.sleep(0.001)
        await coordinator.interrupt()
        result = await task
        await coordinator.close()
        assert result.status == DelegateStatus.STALE
        assert result.payload is None
        assert any(event["event"] == "delegate_suppressed" for event in events)
        assert coordinator.pending_tasks == 0

    asyncio.run(run())


def test_delegate_timeout_and_failure_are_bounded():
    async def run():
        coordinator = RealtimePOCCoordinator("session")
        timeout = await coordinator.start_delegate(
            lambda: delayed_lookup(delay_ms=30, result={}), timeout_ms=1
        )
        failed = await coordinator.start_delegate(
            lambda: delayed_lookup(delay_ms=0, result={}, failure="synthetic"),
            timeout_ms=50,
        )
        await coordinator.close()
        assert timeout.status == DelegateStatus.TIMED_OUT
        assert failed.status == DelegateStatus.FAILED
        assert failed.error_category == "RuntimeError"

    asyncio.run(run())


def test_frozen_scenarios_and_poc_schemas_are_valid():
    scenarios = json.loads(
        (LAB / "voice_eval" / "frozen" / "live-poc-scenarios.v1.json").read_text()
    )
    assert len(scenarios["scenarios"]) == 8
    for name in (
        "realtime-poc-run-result.v1.schema.json",
        "realtime-poc-report.v1.schema.json",
        "realtime-founder-event.v1.schema.json",
        "realtime-founder-checkpoint.v1.schema.json",
    ):
        Draft202012Validator.check_schema(load_json_schema(name))


def test_output_gate_drops_old_generation_text_and_pcm():
    async def run():
        events = []
        pushed = []
        coordinator = RealtimePOCCoordinator("session", event_sink=events.append)
        gate = GenerationOutputGate(coordinator)

        async def capture(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        gate.push_frame = capture
        gate._active_generation = coordinator.generations.current.generation_id
        coordinator.generations.advance_generation()
        late_text = LLMTextFrame(text="late old")
        late_audio = TTSAudioRawFrame(audio=b"\0\0" * 10, sample_rate=24000, num_channels=1)
        await gate.process_frame(late_text, FrameDirection.DOWNSTREAM)
        await gate.process_frame(late_audio, FrameDirection.DOWNSTREAM)
        assert pushed == []
        assert sum(event["event"] == "stale_output_dropped" for event in events) == 2

    asyncio.run(run())
