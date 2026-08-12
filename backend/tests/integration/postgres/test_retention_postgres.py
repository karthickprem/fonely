"""PostgreSQL integration tests for data retention cleanup.

Proves that raw SQL in DataRetentionService correctly deletes old
terminal records, preserves active/recent records, respects FK
references, and honours batch limits — against real PostgreSQL.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.validators import utcnow
from fonely.models.schema import (
    Conversation,
    DBConversationTurn,
    NotificationOutboxEvent,
    PendingAction,
)
from fonely.services.data_retention import DataRetentionService

pytestmark = pytest.mark.postgres

_SEED_SQL = (
    "INSERT INTO businesses (id, name, category, primary_contact_phone, timezone, subscription) "
    "VALUES (1, 'Smile Dental', 'dental_clinic', '+910000000001', 'Asia/Kolkata', 'trial')"
)


async def _seed_business(session: AsyncSession) -> None:
    await session.execute(text(_SEED_SQL))
    await session.flush()


def _conv_id() -> str:
    return str(uuid.uuid4())


async def _insert_conversation(
    session: AsyncSession,
    *,
    state: str = "completed",
    updated_at_offset_days: int = 0,
    with_turns: int = 0,
) -> str:
    conv_id = _conv_id()
    now = utcnow()
    updated = now - timedelta(days=updated_at_offset_days)
    await session.execute(
        text(
            "INSERT INTO conversations "
            "(id, business_id, customer_phone, state, collected_facts, "
            " turn_count, expires_at, created_at, updated_at) "
            "VALUES (:id, 1, '+919123456789', :state, '{}'::jsonb, "
            " :turns, :expires, :created, :updated)"
        ),
        {
            "id": conv_id,
            "state": state,
            "turns": with_turns,
            "expires": now + timedelta(hours=1),
            "created": updated,
            "updated": updated,
        },
    )
    for i in range(with_turns):
        turn_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO conversation_turns "
                "(id, conversation_id, business_id, turn_number, state, "
                " intent, safety_classification, user_message_hash, "
                " assistant_response, collected_facts_snapshot, missing_facts, created_at) "
                "VALUES (:id, :conv, 1, :num, 'greeting', 'booking', 'safe', "
                " :hash, 'response', '{}'::jsonb, '[]'::jsonb, :ts)"
            ),
            {
                "id": turn_id,
                "conv": conv_id,
                "num": i + 1,
                "hash": f"hash_{conv_id}_{i}",
                "ts": updated,
            },
        )
    await session.flush()
    return conv_id


async def _insert_notification(
    session: AsyncSession,
    *,
    status: str = "delivered",
    updated_at_offset_days: int = 0,
    idempotency_suffix: str = "",
) -> int:
    now = utcnow()
    updated = now - timedelta(days=updated_at_offset_days)
    result = await session.execute(
        text(
            "INSERT INTO notification_outbox "
            "(business_id, event_type, entity_type, entity_id, "
            " recipient_type, recipient_phone, channel, payload, "
            " status, idempotency_key, created_at, updated_at) "
            "VALUES (1, 'appointment_confirmed', 'appointment', 1, "
            " 'patient', '+919123456789', 'whatsapp', '{}'::jsonb, "
            " :status, :key, :created, :updated) "
            "RETURNING id"
        ),
        {
            "status": status,
            "key": f"test-notif-{uuid.uuid4()}{idempotency_suffix}",
            "created": updated,
            "updated": updated,
        },
    )
    row = result.one()
    await session.flush()
    return row[0]


class TestConversationCleanup:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_cleanup_old_completed_conversations(self, pg_session: AsyncSession) -> None:
        await _seed_business(pg_session)

        old_completed = await _insert_conversation(
            pg_session, state="completed", updated_at_offset_days=100, with_turns=3
        )
        recent_completed = await _insert_conversation(
            pg_session, state="completed", updated_at_offset_days=10, with_turns=2
        )
        old_active = await _insert_conversation(
            pg_session, state="greeting", updated_at_offset_days=100, with_turns=1
        )

        svc = DataRetentionService(pg_session)
        result = await svc.run_cleanup()

        assert result.conversations_deleted == 1
        assert result.turns_deleted == 3

        remaining = (await pg_session.execute(select(Conversation.id))).scalars().all()
        assert old_completed not in remaining
        assert recent_completed in remaining
        assert old_active in remaining

        old_turns = (
            await pg_session.execute(
                select(func.count())
                .select_from(DBConversationTurn)
                .where(DBConversationTurn.conversation_id == old_completed)
            )
        ).scalar()
        assert old_turns == 0

        recent_turns = (
            await pg_session.execute(
                select(func.count())
                .select_from(DBConversationTurn)
                .where(DBConversationTurn.conversation_id == recent_completed)
            )
        ).scalar()
        assert recent_turns == 2

        active_turns = (
            await pg_session.execute(
                select(func.count())
                .select_from(DBConversationTurn)
                .where(DBConversationTurn.conversation_id == old_active)
            )
        ).scalar()
        assert active_turns == 1


class TestNotificationCleanup:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_cleanup_old_delivered_notifications(self, pg_session: AsyncSession) -> None:
        await _seed_business(pg_session)

        old_delivered_id = await _insert_notification(
            pg_session, status="delivered", updated_at_offset_days=45
        )
        recent_delivered_id = await _insert_notification(
            pg_session, status="delivered", updated_at_offset_days=15
        )
        pending_id = await _insert_notification(
            pg_session, status="pending", updated_at_offset_days=100
        )

        svc = DataRetentionService(pg_session)
        result = await svc.run_cleanup()

        assert result.notifications_deleted >= 1

        remaining_ids = (
            (await pg_session.execute(select(NotificationOutboxEvent.id))).scalars().all()
        )
        assert old_delivered_id not in remaining_ids
        assert recent_delivered_id in remaining_ids
        assert pending_id in remaining_ids


class TestPendingActionProtection:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_cleanup_protects_referenced_pending_actions(
        self, pg_session: AsyncSession
    ) -> None:
        await _seed_business(pg_session)

        now = utcnow()
        old = now - timedelta(days=100)

        await pg_session.execute(
            text(
                "INSERT INTO services (id, business_id, name, duration_minutes, "
                " buffer_before_minutes, buffer_after_minutes, price, is_active) "
                "VALUES (1, 1, 'Consultation', 30, 0, 0, 500, true)"
            )
        )
        await pg_session.execute(
            text(
                "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
                "VALUES (1, 1, 'Dr. Priya', 'dentist', true)"
            )
        )
        await pg_session.flush()

        pa_result = await pg_session.execute(
            text(
                "INSERT INTO pending_actions "
                "(business_id, action_type, payload_schema_version, "
                " proposed_payload, payload_digest, status, "
                " committed_entity_type, committed_entity_id, "
                " expires_at, idempotency_key, "
                " created_at, updated_at, version) "
                "VALUES (1, 'appointment', 1, "
                " '{}'::jsonb, 'test-digest', 'confirmed', "
                " 'appointment', 1, "
                " :expires, 'retention-test-pa', "
                " :old, :old, 1) "
                "RETURNING id"
            ),
            {"old": old, "expires": now + timedelta(hours=1)},
        )
        pa_id = pa_result.scalar_one()

        start = now + timedelta(hours=2)
        end = start + timedelta(minutes=30)
        await pg_session.execute(
            text(
                "INSERT INTO appointments "
                "(business_id, resource_id, service_id, customer_phone, "
                " start_at, end_at, effective_start_at, effective_end_at, "
                " status, source, service_name_snapshot, resource_name_snapshot, "
                " duration_minutes_snapshot, buffer_before_minutes_snapshot, "
                " buffer_after_minutes_snapshot, business_timezone_snapshot, "
                " pending_action_id, idempotency_key) "
                "VALUES (1, 1, 1, '+919123456789', "
                " :start, :end, :start, :end, "
                " 'confirmed', 'customer_conversation', 'Consultation', 'Dr. Priya', "
                " 30, 0, 0, 'Asia/Kolkata', :pa_id, :idem)"
            ),
            {"start": start, "end": end, "pa_id": pa_id, "idem": f"pa-{pa_id}"},
        )
        await pg_session.flush()

        svc = DataRetentionService(pg_session)
        result = await svc.run_cleanup()

        assert result.pending_actions_deleted == 0

        remaining = await pg_session.scalar(
            select(func.count()).select_from(PendingAction).where(PendingAction.id == pa_id)
        )
        assert remaining == 1


async def _insert_call(
    session: AsyncSession,
    *,
    started_offset_days: int,
    ended: bool = True,
    transcript: str | None = '[{"role": "user", "text": "pallu vali"}]',
) -> int:
    """A call row with a transcript, aged by started_offset_days."""
    started = utcnow() - timedelta(days=started_offset_days)
    result = await session.execute(
        text(
            "INSERT INTO calls "
            "(business_id, caller_phone, outcome, transcript, started_at, ended_at) "
            "VALUES (1, '+919123456789', 'booked', "
            " CAST(:transcript AS jsonb), :started, :ended) "
            "RETURNING id"
        ),
        {
            "transcript": transcript,
            "started": started,
            "ended": started + timedelta(minutes=3) if ended else None,
        },
    )
    call_id = result.scalar_one()
    await session.flush()
    return call_id


async def _transcript_of(session: AsyncSession, call_id: int) -> object:
    return await session.scalar(
        text("SELECT transcript FROM calls WHERE id = :id"), {"id": call_id}
    )


class TestCallTranscriptRedaction:
    """The transcript expires; the call row does not.

    Every case here checks the row survived as well as what happened to the
    transcript. A retention pass that quietly deleted `calls` rows would
    destroy the clinic's own record of who rang them, and would still satisfy
    an assertion that only looked at the transcript.
    """

    @pytest.mark.asyncio(loop_scope="session")
    async def test_expired_transcript_is_redacted_and_row_survives(
        self, pg_session: AsyncSession
    ) -> None:
        await _seed_business(pg_session)
        old_id = await _insert_call(pg_session, started_offset_days=100)

        result = await DataRetentionService(pg_session).run_cleanup()
        assert result.call_transcripts_redacted == 1

        row = (
            await pg_session.execute(
                text("SELECT caller_phone, outcome, transcript FROM calls WHERE id = :id"),
                {"id": old_id},
            )
        ).one()
        caller_phone, outcome, transcript = row

        # The patient's words are gone...
        assert "pallu vali" not in str(transcript)
        assert transcript["redacted"] is True
        assert transcript["policy"] == "call_transcripts"
        assert transcript["redacted_at"]

        # ...and the operational record is not.
        assert outcome == "booked"
        # caller_phone is deliberately untouched: phone retention is per
        # clinic instruction, which nobody has configured yet. Redacting it
        # here would be a policy decision taken by accident.
        assert caller_phone == "+919123456789"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_recent_transcript_is_untouched(self, pg_session: AsyncSession) -> None:
        await _seed_business(pg_session)
        recent_id = await _insert_call(pg_session, started_offset_days=10)

        result = await DataRetentionService(pg_session).run_cleanup()
        assert result.call_transcripts_redacted == 0
        assert "pallu vali" in str(await _transcript_of(pg_session, recent_id))

    @pytest.mark.asyncio(loop_scope="session")
    async def test_call_that_never_ended_still_expires(self, pg_session: AsyncSession) -> None:
        """The COALESCE case, and the reason it is not keyed on ended_at.

        A dropped call never gets its completed callback, so ended_at stays
        NULL forever. Keying retention on ended_at alone would exempt exactly
        those transcripts permanently, and the exemption would be invisible --
        the pass would report success every night while never touching them.
        """
        await _seed_business(pg_session)
        stuck_id = await _insert_call(pg_session, started_offset_days=100, ended=False)

        result = await DataRetentionService(pg_session).run_cleanup()
        assert result.call_transcripts_redacted == 1

        transcript = await _transcript_of(pg_session, stuck_id)
        assert "pallu vali" not in str(transcript)
        assert transcript["redacted"] is True  # type: ignore[index]

    @pytest.mark.asyncio(loop_scope="session")
    async def test_redaction_does_not_repeat(self, pg_session: AsyncSession) -> None:
        """A second pass must find nothing, not re-redact and re-count.

        Otherwise the nightly job reports the same rows as freshly purged
        forever, and the count stops meaning anything.
        """
        await _seed_business(pg_session)
        old_id = await _insert_call(pg_session, started_offset_days=100)

        svc = DataRetentionService(pg_session)
        assert (await svc.run_cleanup()).call_transcripts_redacted == 1
        first = await _transcript_of(pg_session, old_id)

        assert (await svc.run_cleanup()).call_transcripts_redacted == 0
        assert await _transcript_of(pg_session, old_id) == first

    @pytest.mark.asyncio(loop_scope="session")
    async def test_call_without_transcript_is_not_counted(self, pg_session: AsyncSession) -> None:
        """A call that never recorded anything is not a purge."""
        await _seed_business(pg_session)
        await _insert_call(pg_session, started_offset_days=100, transcript=None)

        result = await DataRetentionService(pg_session).run_cleanup()
        assert result.call_transcripts_redacted == 0


_DIGEST = "a" * 64

# "not passed" has to be distinguishable from "explicitly NULL": every test
# below that exercises the all-or-none CHECK needs to send a real SQL NULL for
# one field, and a plain `None` default would make that indistinguishable from
# the aged-timestamp default.
_UNSET = object()


async def _insert_call_with_notice(
    session: AsyncSession,
    *,
    started_offset_days: int,
    completed_at: object = _UNSET,
    version: str | None = "1",
    locale: str | None = "ta-IN",
    digest: str | None = _DIGEST,
) -> int:
    """A call carrying DPDP notice evidence, aged by started_offset_days."""
    started = utcnow() - timedelta(days=started_offset_days)
    result = await session.execute(
        text(
            "INSERT INTO calls "
            "(business_id, caller_phone, outcome, transcript, started_at, ended_at, "
            " dpdp_notice_completed_at, dpdp_notice_version, dpdp_notice_locale, "
            " dpdp_notice_content_digest) "
            "VALUES (1, '+919123456789', 'booked', "
            " CAST(:transcript AS jsonb), :started, :ended, "
            " :completed_at, :version, :locale, :digest) "
            "RETURNING id"
        ),
        {
            "transcript": '[{"role": "user", "text": "pallu vali"}]',
            "started": started,
            "ended": started + timedelta(minutes=3),
            "completed_at": started if completed_at is _UNSET else completed_at,
            "version": version,
            "locale": locale,
            "digest": digest,
        },
    )
    call_id = result.scalar_one()
    await session.flush()
    return call_id


async def _evidence_of(session: AsyncSession, call_id: int) -> tuple[object, ...]:
    row = (
        await session.execute(
            text(
                "SELECT dpdp_notice_completed_at, dpdp_notice_version, "
                "dpdp_notice_locale, dpdp_notice_content_digest "
                "FROM calls WHERE id = :id"
            ),
            {"id": call_id},
        )
    ).one()
    return tuple(row)


class TestDpdpNoticeEvidenceSurvivesRetention:
    """The consent evidence outlives the transcript. This is the whole reason
    it is stored in columns.

    The transcript is redacted at 90 days; appointments are retained for 365.
    If the proof that a patient was read the notice lived inside the transcript
    JSONB, the nightly retention pass would delete it, and for the following
    nine months the clinic would hold that patient's booking with no record
    that notice was ever given. That is precisely the state a regulator asks
    about, and it would have arrived on a schedule, silently.

    So this is not a test of the retention SQL. It is the guard on the storage
    decision: if someone later moves the evidence into the transcript, or
    widens the redaction UPDATE to blank these columns, this fails.
    """

    @pytest.mark.asyncio(loop_scope="session")
    async def test_evidence_survives_the_pass_that_redacts_the_transcript(
        self, pg_session: AsyncSession
    ) -> None:
        await _seed_business(pg_session)
        call_id = await _insert_call_with_notice(pg_session, started_offset_days=100)
        before = await _evidence_of(pg_session, call_id)

        result = await DataRetentionService(pg_session).run_cleanup()

        # The pass really ran on this row -- otherwise "evidence survived"
        # would be true of a pass that did nothing at all.
        assert result.call_transcripts_redacted == 1
        transcript = await _transcript_of(pg_session, call_id)
        assert "pallu vali" not in str(transcript)
        assert transcript["redacted"] is True  # type: ignore[index]

        # ...and every one of the four facts is byte-identical afterwards.
        after = await _evidence_of(pg_session, call_id)
        assert after == before
        completed_at, version, locale, digest = after
        assert completed_at is not None
        assert version == "1"
        assert locale == "ta-IN"
        assert digest == _DIGEST

    @pytest.mark.asyncio(loop_scope="session")
    async def test_a_call_with_no_notice_is_still_redacted_normally(
        self, pg_session: AsyncSession
    ) -> None:
        """All-NULL evidence must not exempt a row from retention.

        A call from before the notice existed still holds a patient's words,
        and the redaction predicate must not have acquired a dependency on the
        new columns.
        """
        await _seed_business(pg_session)
        call_id = await _insert_call(pg_session, started_offset_days=100)

        result = await DataRetentionService(pg_session).run_cleanup()
        assert result.call_transcripts_redacted == 1
        assert await _evidence_of(pg_session, call_id) == (None, None, None, None)


class TestDpdpNoticeEvidenceConstraints:
    """Partial or malformed evidence must be unrepresentable in the database.

    These are CHECK constraints rather than application validation because the
    writer is not the only thing that will ever touch this table -- a backfill
    script or a support fix at the psql prompt would bypass Python entirely,
    and a half-written row would read as consent to anyone auditing later.
    """

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("missing", ["completed_at", "version", "locale", "digest"])
    async def test_partial_evidence_is_rejected(
        self, pg_session: AsyncSession, missing: str
    ) -> None:
        await _seed_business(pg_session)
        with pytest.raises(IntegrityError) as excinfo:
            async with pg_session.begin_nested():
                await _insert_call_with_notice(
                    pg_session,
                    started_offset_days=1,
                    completed_at=None if missing == "completed_at" else utcnow(),
                    version=None if missing == "version" else "1",
                    locale=None if missing == "locale" else "ta-IN",
                    digest=None if missing == "digest" else _DIGEST,
                )
        assert "ck_calls_dpdp_notice_all_or_none" in str(excinfo.value)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_all_four_null_is_accepted(self, pg_session: AsyncSession) -> None:
        """History predates the notice; NULL across all four is legal."""
        await _seed_business(pg_session)
        call_id = await _insert_call_with_notice(
            pg_session,
            started_offset_days=1,
            completed_at=None,
            version=None,
            locale=None,
            digest=None,
        )
        assert await _evidence_of(pg_session, call_id) == (None, None, None, None)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_non_hex_digest_is_rejected(self, pg_session: AsyncSession) -> None:
        """A digest that cannot be recomputed is not evidence.

        Upper case and short values are the two forms a careless writer
        actually produces, and both would silently fail to match a
        recomputation years later.
        """
        await _seed_business(pg_session)
        for bad in ("A" * 64, "a" * 63, "z" * 64):
            with pytest.raises(IntegrityError) as excinfo:
                async with pg_session.begin_nested():
                    await _insert_call_with_notice(pg_session, started_offset_days=1, digest=bad)
            assert "ck_calls_dpdp_notice_digest_hex" in str(excinfo.value)


class TestBatchSize:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_batch_size_respected(self, pg_session: AsyncSession) -> None:
        await _seed_business(pg_session)

        for _i in range(1500):
            await _insert_conversation(pg_session, state="completed", updated_at_offset_days=100)
        await pg_session.flush()

        svc = DataRetentionService(pg_session)

        result1 = await svc.run_cleanup()
        assert result1.conversations_deleted == 1000

        count_after_first = await pg_session.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.state == "completed")
        )
        assert count_after_first == 500

        result2 = await svc.run_cleanup()
        assert result2.conversations_deleted == 500

        count_after_second = await pg_session.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.state == "completed")
        )
        assert count_after_second == 0
