"""The enforced session-open order: notice → playback → evidence → greeting →
STT opens. Structural, not a timing race.

DPDP requires that the patient hears the notice, and that we durably record they
heard THIS version, BEFORE we capture a single word. The order is:

    1. speak the notice
    2. wait for notice playback to actually complete
    3. persist the notice-completion evidence (digest of the exact spoken text)
    4. speak the greeting (which re-invites the caller — any speech during the
       notice was dropped at the latch and is not treated as understood)
    5. open the input latch so STT begins to receive caller audio

Failure discipline (a dropped call is a product defect, and capturing speech we
cannot prove consent for is a compliance defect):

  * If notice synthesis/playback fails, or the evidence write fails AFTER
    playback succeeded, the latch STAYS CLOSED, the caller hears a short spoken
    failure line, and the call is torn down. STT never opens.
  * The evidence-write-failure branch is the subtle one: playback succeeded but
    we could not record it, so we must NOT proceed — we have no provable consent.

This module is pure orchestration over injected effects (speak / await-playback /
write-evidence / open-latch), so every branch is unit-testable with fakes and
the outcome is observable. The runtime wires the effects to real TTS, the
transport's playback-complete signal, the evidence writer, and NoticeInputLatch.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .evidence import DpdpEvidenceWriter, notice_content_digest


class OpenOutcome(Enum):
    """The terminal state of the open sequence. Distinct values so a caller (and
    a test) can tell "notice never played" from "evidence write failed" from
    "opened" — absence must not read as success."""

    OPENED = "opened"
    NOTICE_PLAYBACK_FAILED = "notice_playback_failed"
    EVIDENCE_WRITE_FAILED = "evidence_write_failed"


@dataclass(frozen=True)
class OpenResult:
    outcome: OpenOutcome
    stt_opened: bool
    # The digest actually written, when evidence persisted; None otherwise. This
    # is the digest of the EXACT emitted notice text, so a PG proof can assert
    # digest(spoken_text) == stored.
    content_digest: str | None = None


async def run_open_sequence(
    *,
    call_id: int,
    notice_text: str,
    greeting_text: str,
    notice_version: str,
    locale: str,
    speak: Callable[[str], Awaitable[bool]],
    await_playback_complete: Callable[[], Awaitable[bool]],
    evidence_writer: DpdpEvidenceWriter,
    open_latch: Callable[[], None],
    now: Callable[[], datetime],
    speak_failure_line: Callable[[], Awaitable[None]],
) -> OpenResult:
    """Run notice → playback → evidence → greeting → open, enforcing the order.

    ``speak`` returns False if synthesis/playback of that utterance failed.
    ``await_playback_complete`` returns False if the notice did not finish
    playing. On any failure before the latch opens, the caller hears a short
    failure line and STT stays closed.
    """
    # 1-2. Notice, then real playback completion. If either fails, STT never
    # opens; the caller is told, then hung up.
    spoke = await speak(notice_text)
    played = await await_playback_complete() if spoke else False
    if not (spoke and played):
        await speak_failure_line()
        return OpenResult(OpenOutcome.NOTICE_PLAYBACK_FAILED, stt_opened=False)

    # 3. Persist evidence of the EXACT text we just spoke. If this fails, we
    # have unprovable consent — keep the latch closed and tear down.
    digest = notice_content_digest(notice_text, notice_version, locale)
    try:
        await evidence_writer.write(
            call_id=call_id,
            completed_at=now(),
            notice_version=notice_version,
            locale=locale,
            content_digest=digest,
        )
    except Exception:
        await speak_failure_line()
        return OpenResult(OpenOutcome.EVIDENCE_WRITE_FAILED, stt_opened=False)

    # 4. Greeting re-invites the caller (speech during the notice was dropped).
    await speak(greeting_text)

    # 5. Only now does caller audio reach STT.
    open_latch()
    return OpenResult(OpenOutcome.OPENED, stt_opened=True, content_digest=digest)
