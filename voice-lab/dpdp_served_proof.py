"""D3-A served-path proof: the demo's notice open-sequence persists the four
authoritative dpdp_notice_* columns on the call row, via the real
SqlDpdpEvidenceWriter, against the migrated fonely_dev4 (head 0018).

This exercises the ACTUAL served code path the :3000 demo runs on connect —
production_wiring.create_call_row + build_notice_evidence_writer (now
SqlDpdpEvidenceWriter) + fonely.voice.notice_playback.build_notice_open_sequence
+ fonely.voice.open_order.run_open_sequence — not a reimplementation. The only
things faked are the transport-side effects the open order injects (speak /
await-playback / latch), because those need a live browser+TTS we don't have
headless. The EVIDENCE WRITE is real: real writer, real DB, real columns.

Boundary (state it so nobody misquotes it):
  * This proves the served path WRITES the 4 columns on a served-shaped call
    when running the consume branch (dpdp-consume, tag dpdp-consume-d32115c) —
    NOT that integration serves a notice (the delta isn't integrated yet), and
    NOT that a notice was SPOKEN on a real call (that's the real-browser gate).
  * #31 closes only when BOTH: columns written on the served path (this) AND the
    notice spoken on a real WebRTC call (Karthick's gate).

Run: python -P dpdp_served_proof.py   (from the voice-lab dir; needs fonely_dev4
at head 0018 with the dpdp_notice_* columns present).
"""

from __future__ import annotations

import asyncio
import sys

# Same src the demo runs (consume branch checkout).
_RUNTIME_SRC = "/scratch/karthick/fonely/.claude/worktrees/dev4-voice-runtime/backend/src"
if _RUNTIME_SRC not in sys.path:
    sys.path.insert(0, _RUNTIME_SRC)

import production_wiring as pw  # sets DATABASE_URL=fonely_dev4 in-process
from sqlalchemy import text as sql_text

from fonely.voice.evidence import notice_content_digest
from fonely.voice.input_latch import NoticeInputLatch
from fonely.voice.notice_playback import build_notice_open_sequence
from fonely.voice.open_order import OpenOutcome
from fonely.voice.session_open import open_session

LOCALE = "ta-IN"
GREETING = "vanakkam, appointment book panna help pannalaam"


async def _columns_exist() -> bool:
    async with pw._SessionLocal() as session:
        rows = await session.execute(
            sql_text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='calls' AND column_name LIKE 'dpdp_notice_%'"
            )
        )
        found = {r[0] for r in rows}
    need = {
        "dpdp_notice_completed_at",
        "dpdp_notice_version",
        "dpdp_notice_locale",
        "dpdp_notice_content_digest",
    }
    missing = need - found
    if missing:
        print(f"PRECONDITION FAIL: fonely_dev4 missing columns {sorted(missing)} "
              f"— migrate 0015->0018 first")
        return False
    return True


async def _read_evidence(call_id: int):
    async with pw._SessionLocal() as session:
        row = await session.execute(
            sql_text(
                "SELECT dpdp_notice_completed_at, dpdp_notice_version, "
                "       dpdp_notice_locale, dpdp_notice_content_digest "
                "FROM calls WHERE id = :id"
            ),
            {"id": call_id},
        )
        return row.one_or_none()


async def main() -> int:
    conv = "dpdp-served-proof-conv"

    if not await _columns_exist():
        return 2

    # 1. The served path creates the call row (stands in for admission).
    call_id = await pw.create_call_row(conversation_id=conv)
    print(f"created call row id={call_id}")

    # Pre-state: evidence columns must be NULL before the open sequence runs.
    before = await _read_evidence(call_id)
    assert before is not None, f"call row {call_id} not found"
    assert before[0] is None, f"expected NULL dpdp_notice_completed_at, got {before[0]!r}"
    print("pre-state: dpdp_notice_completed_at IS NULL (evidence not yet recorded)")

    # 2. The real evidence writer (SqlDpdpEvidenceWriter against fonely_dev4).
    evidence_writer = pw.build_notice_evidence_writer()
    assert type(evidence_writer).__name__ == "SqlDpdpEvidenceWriter", (
        f"expected SqlDpdpEvidenceWriter, got {type(evidence_writer).__name__} "
        f"— the writer swap did not take effect"
    )
    print(f"writer = {type(evidence_writer).__name__} (real SQL writer)")

    # 3. Build the SAME open sequence the connect handler builds. Fake only the
    #    transport effects (speak/playback/latch); the evidence write is real.
    opening = open_session(
        clinic_name="Smile Care Dental Clinic", greeting_text=GREETING, locale=LOCALE
    )
    latch = NoticeInputLatch()
    spoken: list[str] = []

    async def queue_frames(frames):
        spoken.extend(frames)

    def make_speech_frames(text: str):
        return [text]  # the "frame" is the line itself for this proof

    async def await_playback_complete() -> bool:
        return True  # notice playback succeeds (real playback = browser gate)

    from datetime import datetime
    from zoneinfo import ZoneInfo

    open_sequence = build_notice_open_sequence(
        call_id=call_id,
        opening=opening,
        locale=LOCALE,
        queue_frames=queue_frames,
        make_speech_frames=make_speech_frames,
        await_playback_complete=await_playback_complete,
        evidence_writer=evidence_writer,
        latch=latch,
        now=lambda: datetime.now(ZoneInfo("Asia/Kolkata")),
        failure_line="failure",
    )

    result = await open_sequence()

    # 4. Assert the served path OPENED and wrote the four columns durably.
    assert result.outcome is OpenOutcome.OPENED, f"open failed: {result.outcome}"
    assert latch.is_open is True, "latch must be open after successful evidence write"

    after = await _read_evidence(call_id)
    completed_at, version, locale, digest = after
    expected_digest = notice_content_digest(
        opening.notice_text, opening.notice_version, LOCALE
    )

    assert completed_at is not None, "dpdp_notice_completed_at NOT written"
    assert version == opening.notice_version, f"version {version!r} != {opening.notice_version!r}"
    assert locale == LOCALE, f"locale {locale!r} != {LOCALE!r}"
    assert digest == expected_digest, (
        f"digest mismatch: stored {digest!r} != digest-of-spoken {expected_digest!r}"
    )
    # The digest is of the EXACT spoken notice text — tamper-evident proof of
    # what the patient was read, not merely that something was recorded.
    assert opening.notice_text in spoken, "notice text was not the first thing queued"

    print("=" * 60)
    print("SERVED-PATH DPDP COLUMN-WRITE PROOF — PASS")
    print(f"  call_id                 = {call_id}")
    print(f"  dpdp_notice_completed_at= {completed_at.isoformat()}")
    print(f"  dpdp_notice_version     = {version}")
    print(f"  dpdp_notice_locale      = {locale}")
    print(f"  dpdp_notice_content_digest = {digest}")
    print(f"  digest == sha256(exact spoken notice) : {digest == expected_digest}")
    print("=" * 60)
    print("BOUNDARY: proves the served path (consume branch d32115c) WRITES the")
    print("4 columns. Does NOT prove integration serves a notice, nor that a")
    print("notice was SPOKEN on a real call (Karthick real-browser gate). #31")
    print("closes only when BOTH hold.")

    # Clean up the proof row so repeated runs don't accrete.
    async with pw._SessionLocal() as session:
        await session.execute(sql_text("DELETE FROM calls WHERE id = :id"), {"id": call_id})
        await session.commit()
    print(f"cleaned up proof row {call_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
