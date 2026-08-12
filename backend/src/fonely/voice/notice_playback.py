"""Bind the enforced open order to a Pipecat frame queue + the input latch.

A transport ``on_client_connected`` handler must speak the DPDP notice, await
its real playback, persist the completion evidence, and ONLY THEN speak the
greeting and open capture. The bug this closes is the one the lab demo shipped:
the greeting frames were enqueued UNCONDITIONALLY (and there was no capture
latch at all), so a failed evidence write still greeted the caller and let STT
receive audio — consent we could not prove.

``run_open_sequence`` (open_order.py) already enforces the ordering over injected
effects; this module supplies the four effects for a Pipecat worker so the
greeting is enqueued strictly on the success path and capture opens only after
evidence persists:

  * ``speak(text)``   → enqueue the synthesis frames for ``text`` onto the worker
  * ``await_playback_complete`` → wait for the notice to actually finish playing
  * ``evidence_writer`` → the DpdpEvidenceWriter port (Fake in tests, SQL later)
  * ``open_latch``    → flip the NoticeInputLatch (the structural capture gate)

The returned closure has the exact shape ``CallComponents.open_sequence`` wants
(``() -> Awaitable[OpenResult]``), so the runtime and the lab handler drive it
the same way. Because ``speak(greeting)`` is only reached on the success path,
the greeting frames are provably absent from the queue on any failure — the
property the fail-injection test asserts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .open_order import run_open_sequence

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from .evidence import DpdpEvidenceWriter
    from .input_latch import NoticeInputLatch
    from .open_order import OpenResult
    from .session_open import SessionOpening


def build_notice_open_sequence(
    *,
    call_id: int,
    opening: SessionOpening,
    locale: str,
    queue_frames: Callable[[Sequence[object]], Awaitable[None]],
    make_speech_frames: Callable[[str], Sequence[object]],
    await_playback_complete: Callable[[], Awaitable[bool]],
    evidence_writer: DpdpEvidenceWriter,
    latch: NoticeInputLatch,
    now: Callable[[], datetime],
    failure_line: str,
) -> Callable[[], Awaitable[OpenResult]]:
    """Bind the open order to a frame queue + latch and return the open closure.

    ``make_speech_frames`` turns a line into the concrete synthesis frames for
    this transport (e.g. LLMFullResponseStart / LLMText / LLMFullResponseEnd);
    ``queue_frames`` enqueues them on the worker. ``speak`` reports False if the
    enqueue itself raises, so a broken transport is treated as a playback
    failure (STT stays closed) rather than surfacing as an unhandled error mid
    open. The evidence is written of the EXACT notice text in ``opening`` — the
    same string that was spoken — so the persisted digest proves what was said.
    """

    async def speak(text: str) -> bool:
        # Enqueue the synthesis frames for one line. A failure to enqueue is a
        # playback failure: return False so the sequence fails closed rather
        # than proceeding to open capture.
        try:
            await queue_frames(make_speech_frames(text))
        except Exception:
            return False
        return True

    async def speak_failure_line() -> None:
        # Best-effort: the caller must hear SOMETHING before the call is torn
        # down (a silent dead socket is a product defect). If even this enqueue
        # fails there is nothing more to do — the call still tears down closed.
        try:
            await queue_frames(make_speech_frames(failure_line))
        except Exception:
            return

    return lambda: run_open_sequence(
        call_id=call_id,
        notice_text=opening.notice_text,
        greeting_text=opening.greeting_text,
        notice_version=opening.notice_version,
        locale=locale,
        speak=speak,
        await_playback_complete=await_playback_complete,
        evidence_writer=evidence_writer,
        open_latch=latch.open,
        now=now,
        speak_failure_line=speak_failure_line,
    )
