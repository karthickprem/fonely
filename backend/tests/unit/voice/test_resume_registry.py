"""#43: the cross-call resume registry + its five guards.

Proves that an out-of-band owner reply can drive proactive speech into exactly
the right live call, exactly once, and never into the wrong tenant or a dead
call. The registry is the missing object #43 adds; these tests pin its contract
against the real ResumeRegistry/ResumeHandle (no mocks of the unit under test).
"""

from __future__ import annotations

import asyncio

import pytest

from fonely.voice.resume_registry import ResumeHandle, ResumeRegistry, make_speech_frames


def _handle(*, business_id: int, call_id: int, sink: list[object]) -> ResumeHandle:
    async def queue_frames(frames: object) -> None:
        sink.append(frames)

    return ResumeHandle(
        business_id=business_id,
        call_id=call_id,
        _queue_frames=queue_frames,
    )


class TestSpeechFrames:
    def test_make_speech_frames_is_the_assistant_turn_triple(self):
        frames = list(make_speech_frames("hello"))
        # start, one text frame carrying the text, end — the same shape the gate
        # emits and the open sequence injects.
        assert len(frames) == 3
        assert any(getattr(f, "text", None) == "hello" for f in frames)


class TestResumeSpeaksIntoTheCall:
    @pytest.mark.asyncio
    async def test_resume_queues_the_answer_into_the_registered_call(self):
        sink: list[object] = []
        reg = ResumeRegistry()
        await reg.register(_handle(business_id=1, call_id=10, sink=sink))

        assert await reg.resume(1, 10, "Doctor is free at 5pm") is True
        assert len(sink) == 1  # the answer was queued into the call's pipeline


class TestGuardExactlyOnce:
    @pytest.mark.asyncio
    async def test_second_sequential_resume_is_ignored(self):
        sink: list[object] = []
        reg = ResumeRegistry()
        await reg.register(_handle(business_id=1, call_id=10, sink=sink))

        assert await reg.resume(1, 10, "first") is True
        assert await reg.resume(1, 10, "second") is False  # already resumed
        assert len(sink) == 1  # spoken exactly once

    @pytest.mark.asyncio
    async def test_concurrent_resumes_race_resume_exactly_once(self):
        # THE race test: two owner replies hit resume at the SAME time. The CAS
        # under the lock must let exactly one win — never two speech injections.
        sink: list[object] = []
        reg = ResumeRegistry()
        await reg.register(_handle(business_id=1, call_id=10, sink=sink))

        results = await asyncio.gather(
            reg.resume(1, 10, "A"),
            reg.resume(1, 10, "B"),
            reg.resume(1, 10, "C"),
        )
        assert results.count(True) == 1  # exactly one resume won
        assert results.count(False) == 2
        assert len(sink) == 1  # and the call heard exactly one answer


class TestGuardStale:
    @pytest.mark.asyncio
    async def test_resume_of_deregistered_call_is_noop(self):
        sink: list[object] = []
        reg = ResumeRegistry()
        await reg.register(_handle(business_id=1, call_id=10, sink=sink))
        await reg.deregister(1, 10)  # call ended

        assert await reg.resume(1, 10, "too late") is False
        assert sink == []  # nothing spoken into a call that already ended

    @pytest.mark.asyncio
    async def test_resume_of_never_registered_call_is_noop(self):
        reg = ResumeRegistry()
        assert await reg.resume(1, 999, "who?") is False


class TestGuardTenantBound:
    @pytest.mark.asyncio
    async def test_business_a_reply_does_not_resume_business_b_call(self):
        # Same call_id under two businesses. An owner reply keyed to A must NEVER
        # reach B's call — the (business_id, call_id) key is the tenant boundary.
        sink_a: list[object] = []
        sink_b: list[object] = []
        reg = ResumeRegistry()
        await reg.register(_handle(business_id=1, call_id=10, sink=sink_a))
        await reg.register(_handle(business_id=2, call_id=10, sink=sink_b))

        assert await reg.resume(1, 10, "for A") is True
        assert len(sink_a) == 1
        assert sink_b == []  # B's call untouched by A's reply

        assert await reg.resume(2, 10, "for B") is True
        assert len(sink_b) == 1

    @pytest.mark.asyncio
    async def test_reply_for_absent_tenant_does_not_hit_other_tenant(self):
        sink_b: list[object] = []
        reg = ResumeRegistry()
        await reg.register(_handle(business_id=2, call_id=10, sink=sink_b))
        # A reply for business 1 call 10 — business 1 has no such call.
        assert await reg.resume(1, 10, "for A") is False
        assert sink_b == []


class TestGuardTimeoutFallback:
    @pytest.mark.asyncio
    async def test_timeout_speaks_fallback_when_no_reply(self):
        sink: list[object] = []
        handle = _handle(business_id=1, call_id=10, sink=sink)
        handle.arm_timeout(timeout_seconds=0.02, fallback_text="callback shortly")
        await asyncio.sleep(0.06)  # let the timeout fire
        assert len(sink) == 1  # the fallback was spoken

    @pytest.mark.asyncio
    async def test_resume_cancels_the_timeout_no_double_speak(self):
        # A real owner reply lands before the timeout: the fallback must be
        # cancelled-and-awaited so the caller hears the answer, NOT the answer
        # then a stray fallback.
        sink: list[object] = []
        handle = _handle(business_id=1, call_id=10, sink=sink)
        handle.arm_timeout(timeout_seconds=0.05, fallback_text="fallback")
        assert await handle.resume("real answer") is True
        await asyncio.sleep(0.08)  # past when the timeout would have fired
        assert len(sink) == 1  # exactly one utterance — the real answer, no fallback

    @pytest.mark.asyncio
    async def test_arm_timeout_is_idempotent_no_stacked_timers(self):
        sink: list[object] = []
        handle = _handle(business_id=1, call_id=10, sink=sink)
        handle.arm_timeout(timeout_seconds=0.02, fallback_text="one")
        handle.arm_timeout(timeout_seconds=0.02, fallback_text="two")  # no-op
        await asyncio.sleep(0.06)
        assert len(sink) == 1  # only one fallback, not two


class TestGuardHangupDeregisters:
    @pytest.mark.asyncio
    async def test_deregister_cancels_pending_timeout(self):
        # A caller hangup mid-suspension: deregister must cancel the armed
        # timeout so no fallback fires into a torn-down call.
        sink: list[object] = []
        handle = _handle(business_id=1, call_id=10, sink=sink)
        reg = ResumeRegistry()
        await reg.register(handle)
        handle.arm_timeout(timeout_seconds=0.03, fallback_text="fallback")

        await reg.deregister(1, 10)  # hangup
        await asyncio.sleep(0.06)  # past the timeout window
        assert sink == []  # nothing spoken into the dead call
        assert await reg.resume(1, 10, "late") is False  # and it's gone from the map
