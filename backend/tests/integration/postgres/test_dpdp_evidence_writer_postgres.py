"""SqlDpdpEvidenceWriter against the real 0018 columns, through its real path.

The claim this file has to earn is narrow and load-bearing: after the notice
finishes playing, the fact that THIS patient heard THIS notice version is
durably recorded before a single word of their speech is captured. Every part of
that sentence is testable only against a real database -- the digest column is
CHECK-constrained, the write has to survive its own transaction ending, and the
all-or-none constraint is enforced by PostgreSQL and by nothing in Python.

So these tests use the real writer, the real ``calls`` table at revision 0018,
and read every assertion back through a SEPARATE session. Reading back through
the writer's own session would prove only that the statement executed; the
guarantee is that it committed, and those are different claims.

The last class drives ``run_open_sequence`` with the real writer wired in, which
is the only test in the suite where the ordering module and the SQL writer meet.
It asserts the stored digest equals a digest recomputed over the exact text the
fake TTS was asked to speak -- so a future change that lets the notice text and
the recorded digest drift apart fails here rather than in an audit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.voice.evidence import (
    DpdpEvidenceWriteError,
    SqlDpdpEvidenceWriter,
    notice_content_digest,
)
from fonely.voice.open_order import OpenOutcome, run_open_sequence

pytestmark = pytest.mark.postgres

BUSINESS_ID = 941
CALL_ID = 9410
NOW = datetime(2026, 8, 12, 10, 30, tzinfo=UTC)
NOTICE = "வணக்கம். இது Smile Dental Clinic. உங்கள் விவரங்கள் பதிவு செய்யப்படும்."
VERSION = "1"
LOCALE = "ta-IN"


async def _seed_call(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """A business and one in-flight call, with no notice evidence yet.

    ``outcome`` is deliberately left NULL rather than set to a finished value.
    The notice plays at the very start of a call, when no outcome exists yet, so
    seeding a completed call would test the writer against a row shape it will
    never actually meet in production.
    """
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses "
                "(id, name, category, primary_contact_phone, timezone, subscription) "
                "VALUES (:bid, 'Smile Dental Clinic', 'dental_clinic', "
                "'+919000000941', 'Asia/Kolkata', 'trial')"
            ),
            {"bid": BUSINESS_ID},
        )
        await session.execute(
            text(
                "INSERT INTO calls (id, business_id, caller_phone, started_at) "
                "VALUES (:cid, :bid, '+919000000942', :ts)"
            ),
            {"cid": CALL_ID, "bid": BUSINESS_ID, "ts": NOW},
        )
        await session.commit()


async def _read_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[datetime | None, str | None, str | None, str | None]:
    """Read the four columns back through a session the writer never touched."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT dpdp_notice_completed_at, dpdp_notice_version, "
                "       dpdp_notice_locale, dpdp_notice_content_digest "
                "FROM calls WHERE id = :cid"
            ),
            {"cid": CALL_ID},
        )
        row = result.one()
        return (row[0], row[1], row[2], row[3])


class TestEvidenceIsPersisted:
    async def test_all_four_columns_land_together(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_call(pg_session_factory)
        digest = notice_content_digest(NOTICE, VERSION, LOCALE)

        writer = SqlDpdpEvidenceWriter(session_factory=pg_session_factory)
        await writer.write(
            call_id=CALL_ID,
            completed_at=NOW,
            notice_version=VERSION,
            locale=LOCALE,
            content_digest=digest,
        )

        completed_at, version, locale, stored_digest = await _read_evidence(pg_session_factory)
        # Read back through a different session: this is the durability claim,
        # not just "the UPDATE ran".
        assert completed_at == NOW
        assert version == VERSION
        assert locale == LOCALE
        assert stored_digest == digest

    async def test_missing_call_raises_and_writes_nothing(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # No seed. A silent no-op here would open STT with no evidence at all,
        # which is the exact failure the Protocol forbids.
        writer = SqlDpdpEvidenceWriter(session_factory=pg_session_factory)

        with pytest.raises(DpdpEvidenceWriteError, match="no call row"):
            await writer.write(
                call_id=CALL_ID,
                completed_at=NOW,
                notice_version=VERSION,
                locale=LOCALE,
                content_digest=notice_content_digest(NOTICE, VERSION, LOCALE),
            )

        async with pg_session_factory() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM calls WHERE id = :cid"), {"cid": CALL_ID}
            )
        assert count == 0

    async def test_naive_completed_at_is_refused_before_the_database_sees_it(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The one check the database cannot make for us.

        timestamptz would accept a naive datetime and coerce it using the
        session TimeZone, storing a real timestamp that is simply wrong by
        hours. Nothing would error, and the evidence would quietly claim the
        notice completed at a time it did not.
        """
        await _seed_call(pg_session_factory)
        writer = SqlDpdpEvidenceWriter(session_factory=pg_session_factory)

        with pytest.raises(DpdpEvidenceWriteError, match="naive completed_at"):
            await writer.write(
                call_id=CALL_ID,
                completed_at=datetime(2026, 8, 12, 10, 30),  # naive on purpose
                notice_version=VERSION,
                locale=LOCALE,
                content_digest=notice_content_digest(NOTICE, VERSION, LOCALE),
            )

        assert await _read_evidence(pg_session_factory) == (None, None, None, None)

    async def test_malformed_digest_is_rejected_by_the_database_check(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Proves the split-validation line is real.

        The writer deliberately does not re-check the digest format in Python.
        That is only safe if the CHECK genuinely enforces it, so assert the
        database is the one that refuses -- otherwise the omission in the writer
        would be an unguarded hole rather than a deliberate delegation.
        """
        await _seed_call(pg_session_factory)
        writer = SqlDpdpEvidenceWriter(session_factory=pg_session_factory)

        with pytest.raises(Exception, match="ck_calls_dpdp_notice_digest_hex"):
            await writer.write(
                call_id=CALL_ID,
                completed_at=NOW,
                notice_version=VERSION,
                locale=LOCALE,
                content_digest="NOT-A-SHA256",
            )

        assert await _read_evidence(pg_session_factory) == (None, None, None, None)


class TestRepeatWrites:
    async def test_identical_retry_succeeds_and_keeps_the_first_completion_time(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A retry whose first attempt committed before its response was lost.

        It must succeed -- the evidence it wanted is durable -- and it must not
        move completed_at, because the first completion is the one the patient
        actually experienced.
        """
        await _seed_call(pg_session_factory)
        digest = notice_content_digest(NOTICE, VERSION, LOCALE)
        writer = SqlDpdpEvidenceWriter(session_factory=pg_session_factory)

        await writer.write(
            call_id=CALL_ID,
            completed_at=NOW,
            notice_version=VERSION,
            locale=LOCALE,
            content_digest=digest,
        )
        await writer.write(
            call_id=CALL_ID,
            completed_at=NOW + timedelta(minutes=5),
            notice_version=VERSION,
            locale=LOCALE,
            content_digest=digest,
        )

        completed_at, _, _, stored_digest = await _read_evidence(pg_session_factory)
        assert completed_at == NOW
        assert stored_digest == digest

    async def test_different_notice_is_refused_rather_than_overwritten(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Overwriting would replace a true statement about what this patient
        heard with a different one. The stored row must survive untouched."""
        await _seed_call(pg_session_factory)
        first = notice_content_digest(NOTICE, VERSION, LOCALE)
        writer = SqlDpdpEvidenceWriter(session_factory=pg_session_factory)

        await writer.write(
            call_id=CALL_ID,
            completed_at=NOW,
            notice_version=VERSION,
            locale=LOCALE,
            content_digest=first,
        )

        with pytest.raises(DpdpEvidenceWriteError, match="already has different notice evidence"):
            await writer.write(
                call_id=CALL_ID,
                completed_at=NOW,
                notice_version="2",
                locale=LOCALE,
                content_digest=notice_content_digest("a different notice", "2", LOCALE),
            )

        completed_at, version, locale, stored_digest = await _read_evidence(pg_session_factory)
        assert (completed_at, version, locale, stored_digest) == (NOW, VERSION, LOCALE, first)


class TestThroughTheOpenSequence:
    """The ordering module and the real writer, together.

    Everywhere else in the suite run_open_sequence runs against
    FakeEvidenceWriter. This is the only place the sequence's notion of "the
    text we spoke" meets the column that has to prove it years later.
    """

    async def test_stored_digest_matches_the_exact_text_that_was_spoken(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_call(pg_session_factory)
        spoken: list[str] = []

        async def speak(line: str) -> bool:
            spoken.append(line)
            return True

        latch_opened = False

        def open_latch() -> None:
            nonlocal latch_opened
            latch_opened = True

        result = await run_open_sequence(
            call_id=CALL_ID,
            notice_text=NOTICE,
            greeting_text="எப்படி உதவ முடியும்?",
            notice_version=VERSION,
            locale=LOCALE,
            speak=speak,
            await_playback_complete=lambda: _true(),
            evidence_writer=SqlDpdpEvidenceWriter(session_factory=pg_session_factory),
            open_latch=open_latch,
            now=lambda: NOW,
            speak_failure_line=_noop,
        )

        assert result.outcome is OpenOutcome.OPENED
        assert result.stt_opened is True
        assert latch_opened is True
        # The notice was spoken first and the greeting second -- capture is
        # invited only after the notice, never before it.
        assert spoken[0] == NOTICE

        completed_at, version, locale, stored_digest = await _read_evidence(pg_session_factory)
        assert completed_at == NOW
        assert (version, locale) == (VERSION, LOCALE)
        # Recomputed over the text actually handed to TTS, not over a constant:
        # if the two ever drift, this is where it surfaces.
        assert stored_digest == notice_content_digest(spoken[0], VERSION, LOCALE)

    async def test_a_call_that_cannot_be_recorded_never_opens_stt(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """No calls row, so the real writer raises. Playback succeeded and the
        latch must still stay closed -- consent that cannot be proven is not
        consent, and this is the branch where a silent failure would be worst.
        """
        # Deliberately not seeded.
        latch_opened = False

        def open_latch() -> None:  # pragma: no cover - must never run
            nonlocal latch_opened
            latch_opened = True

        failure_lines: list[str] = []

        async def speak_failure_line() -> None:
            failure_lines.append("spoken")

        result = await run_open_sequence(
            call_id=CALL_ID,
            notice_text=NOTICE,
            greeting_text="எப்படி உதவ முடியும்?",
            notice_version=VERSION,
            locale=LOCALE,
            speak=lambda _line: _true(),
            await_playback_complete=lambda: _true(),
            evidence_writer=SqlDpdpEvidenceWriter(session_factory=pg_session_factory),
            open_latch=open_latch,
            now=lambda: NOW,
            speak_failure_line=speak_failure_line,
        )

        assert result.outcome is OpenOutcome.EVIDENCE_WRITE_FAILED
        assert result.stt_opened is False
        assert result.content_digest is None
        assert latch_opened is False
        assert failure_lines == ["spoken"]


async def _true() -> bool:
    return True


async def _noop() -> None:
    return None
