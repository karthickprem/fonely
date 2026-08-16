"""Cross-call resume registry — the seam an OUT-OF-BAND owner reply uses to
drive proactive agent speech into a specific live call.

The problem (#43, found by a live call): the agent escalates to the owner
(asks about availability it can't confirm), the owner replies on their panel,
the reply is stored — but the caller hears NOTHING until they speak again. The
turn loop is the only driver of agent speech: BookingStateInjector fires only
on a patient STT turn, so an out-of-band owner reply never makes the agent
speak. And nothing outside the call frame retains a handle to the live call, so
an owner-reply event has no object to reach.

This module is that missing object. A per-process registry (living on the
mounted VoiceAudioRuntime) maps a call's DURABLE key — ``(business_id,
call_id)``, both from the trusted admitted ``AudioSession``, NEVER from the
owner-reply payload — to a ``ResumeHandle`` that can inject speech into that
exact running pipeline via the SAME proven out-of-band path the open sequence
uses (``PipelineTask.queue_frames`` of an assistant-turn frame triple), and
nothing else.

Tenant isolation is structural: the key carries ``business_id`` from admission,
so a resume request for business A can only ever look up A's call — an owner
reply cannot be routed to another tenant's call even if the payload lies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

logger = logging.getLogger("fonely.voice.resume_registry")


def make_speech_frames(text: str) -> Sequence[object]:
    """The assistant-turn frame triple that speaks ``text`` when queued onto a
    running pipeline. Identical to the shape the post-LLM gate emits and the
    open sequence injects — reused so proactive resume speaks by exactly the
    same path, inventing no new mechanism."""
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
    )

    return [
        LLMFullResponseStartFrame(),
        LLMTextFrame(text=text),
        LLMFullResponseEndFrame(),
    ]


@dataclass
class ResumeHandle:
    """The capability an owner-reply event gets for ONE live call: the power to
    speak into it once, and nothing more.

    Deliberately narrow — it holds a ``queue_frames`` CALLABLE, not the raw
    pipeline task, so a caller can only inject a speech turn, never cancel the
    task, drain frames, or reach transport internals. ``business_id`` is retained
    only to re-assert the tenant match at resume time (defence in depth on top of
    the keyed lookup). ``resumed_once`` + the lock make the resume exactly-once
    under concurrency.
    """

    business_id: int
    call_id: int
    _queue_frames: Callable[[Sequence[object]], Awaitable[None]]
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    resumed_once: bool = False
    # The per-call timeout awaiter (Option A): a task spawned at escalation that
    # speaks a caller-facing fallback if the owner never replies. Cancelled AND
    # awaited on resume or teardown so it never leaks and never races a real
    # resume. None until an escalation arms it.
    timeout_task: asyncio.Task[None] | None = None

    async def resume(self, text: str) -> bool:
        """Speak ``text`` into this call — EXACTLY ONCE across concurrent callers.

        Returns True if this call performed the resume, False if the call was
        already resumed (a duplicate/racing owner reply). The compare-and-set is
        under the lock, so two concurrent owner replies resume once, not twice.
        On the winning path the timeout awaiter is cancelled-and-awaited first,
        so a real resume can never race its own fallback.
        """
        async with self._lock:
            if self.resumed_once:
                return False
            self.resumed_once = True
        # Outside the lock (queue_frames may await the pipeline): the flag is
        # already set, so a second caller has already returned False above.
        await self._cancel_timeout()
        await self._queue_frames(make_speech_frames(text))
        logger.info(
            "call_resumed", extra={"business_id": self.business_id, "call_id": self.call_id}
        )
        return True

    def arm_timeout(
        self,
        *,
        timeout_seconds: float,
        fallback_text: str,
    ) -> None:
        """Arm the guard-(c) fallback: if no owner reply resumes this call within
        ``timeout_seconds``, speak ``fallback_text`` into the call so the caller
        is never left in silence. Called at ESCALATION (when the agent asks the
        owner). Idempotent per call — a second arm while one is pending is a
        no-op, so repeated escalations don't stack timers. The awaiter is
        cancelled-and-awaited by ``resume``/``_cancel_timeout`` so a real reply
        never races its own fallback.
        """
        if self.timeout_task is not None and not self.timeout_task.done():
            return
        self.timeout_task = asyncio.ensure_future(
            self._timeout_fallback(timeout_seconds, fallback_text)
        )

    async def _timeout_fallback(self, timeout_seconds: float, fallback_text: str) -> None:
        try:
            await asyncio.sleep(timeout_seconds)
        except asyncio.CancelledError:
            return
        # Timed out with no owner reply. Speak the fallback — but only if the
        # call hasn't ALREADY been resumed (the CAS makes this exactly-once with
        # a reply landing in the same instant).
        async with self._lock:
            if self.resumed_once:
                return
            self.resumed_once = True
        await self._queue_frames(make_speech_frames(fallback_text))
        logger.info(
            "call_resume_timeout_fallback",
            extra={"business_id": self.business_id, "call_id": self.call_id},
        )

    async def _cancel_timeout(self) -> None:
        """Cancel AND await the timeout awaiter, so no task leaks and no fallback
        fires after a real resume. Awaiting the CancelledError is required — a
        bare cancel would let the fallback race the resume."""
        task = self.timeout_task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class ResumeRegistry:
    """Process-local map from a call's trusted ``(business_id, call_id)`` to its
    ``ResumeHandle``. Lives on the mounted ``VoiceAudioRuntime`` (which outlives
    individual calls), so an owner-reply handler can look up the exact live call.

    Register after the open sequence succeeds; deregister on EVERY terminal path
    (graceful end, hangup, compose/open failure) — the caller does this in the
    same ``finally`` that releases the admission slot. That single deregistration
    point is what makes the ``stale`` and ``hangup`` guards fall out for free: a
    reply for a call no longer in the map is a lookup miss and a safe no-op.
    """

    def __init__(self) -> None:
        self._handles: dict[tuple[int, int], ResumeHandle] = {}
        self._lock = asyncio.Lock()

    async def register(self, handle: ResumeHandle) -> None:
        key = (handle.business_id, handle.call_id)
        async with self._lock:
            self._handles[key] = handle

    async def deregister(self, business_id: int, call_id: int) -> ResumeHandle | None:
        """Remove and return the handle (or None if already gone). Idempotent:
        every terminal path can call it, and a double call is a harmless no-op.
        Also cancels the handle's timeout awaiter so teardown leaves nothing
        running (guard d: a hangup mid-suspension resumes into no dead call)."""
        key = (business_id, call_id)
        async with self._lock:
            handle = self._handles.pop(key, None)
        if handle is not None:
            await handle._cancel_timeout()
        return handle

    async def get(self, business_id: int, call_id: int) -> ResumeHandle | None:
        async with self._lock:
            return self._handles.get((business_id, call_id))

    async def resume(self, business_id: int, call_id: int, text: str) -> bool:
        """Look up the live call by its TRUSTED key and speak ``text`` into it.

        ``business_id`` MUST come from the trusted side (the admitted session
        that owns the escalation), never the owner-reply payload — the key is the
        tenant boundary. Returns True only if a live, not-yet-resumed call for
        this exact tenant+call was found and spoken into; False on a stale/unknown
        key (guard b) or a duplicate resume (guard a).
        """
        handle = await self.get(business_id, call_id)
        if handle is None:
            return False
        # Defence in depth: the keyed lookup already guarantees the tenant match,
        # but re-assert it so a future refactor can't silently cross tenants.
        if handle.business_id != business_id:
            return False
        return await handle.resume(text)

    async def local_keys(self) -> list[tuple[int, int]]:
        """A snapshot of the (business_id, call_id) keys THIS replica currently
        holds. The claim loop uses it to filter its marker query to calls this
        process is actually serving — the replica-ownership boundary. A call this
        replica does not hold is simply absent, so its RESUME_REQUESTED marker is
        left untouched for the owning replica (never claimed here, never resolved
        stale here)."""
        async with self._lock:
            return list(self._handles.keys())

    def active_count(self) -> int:
        return len(self._handles)
