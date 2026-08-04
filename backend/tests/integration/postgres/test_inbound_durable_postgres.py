"""Real PostgreSQL contracts for durable WhatsApp inbox correctness."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.api.channels.whatsapp import (
    _persist_delivery_status,
    _persist_inbound_event,
)
from fonely.models.schema import (
    NotificationOutboxEvent,
    WhatsAppDeliveryAttempt,
    WhatsAppInboundEvent,
)
from fonely.repositories.inbound_events import (
    InboundEventRepository,
    StaleClaimError,
)
from fonely.repositories.notifications import NotificationRepository
from fonely.services.data_retention import DataRetentionService
from fonely.services.whatsapp_notification_sender import DeliveryReceipt
from fonely.workers.inbound_worker import ClaimedEvent, _enqueue_response
from fonely.workers.notification_worker import (
    _claim_one,
    _deliver_claimed,
    _record_accepted,
)

pytestmark = pytest.mark.postgres


async def seed_business(session: AsyncSession, business_id: int = 1) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:id, :name, 'dental_clinic', :phone, 'Asia/Kolkata', 'trial')"
        ),
        {
            "id": business_id,
            "name": f"Clinic {business_id}",
            "phone": f"+9190000000{business_id:02d}",
        },
    )
    await session.flush()


async def insert_event(
    session: AsyncSession,
    *,
    message_id: str,
    sender: str = "919876543210",
    business_id: int = 1,
    status: str = "received",
    attempts: int = 0,
    max_attempts: int = 5,
    provider_timestamp: datetime | None = None,
    next_attempt_at: datetime | None = None,
    message_body: str | None = "book consultation",
) -> int:
    provider_timestamp = provider_timestamp or datetime.now(UTC)
    result = await session.execute(
        text(
            "INSERT INTO whatsapp_inbound_events "
            "(message_id, business_id, phone_number_id, sender_phone, "
            " message_type, message_body, status, attempts, max_attempts, "
            " provider_timestamp, next_attempt_at) "
            "VALUES (:mid, :bid, :pnid, :sender, 'text', :body, :status, "
            " :attempts, :max_attempts, :provider_ts, :next_at) RETURNING id"
        ),
        {
            "mid": message_id,
            "bid": business_id,
            "pnid": f"phone-{business_id}",
            "sender": sender,
            "body": message_body,
            "status": status,
            "attempts": attempts,
            "max_attempts": max_attempts,
            "provider_ts": provider_timestamp,
            "next_at": next_attempt_at,
        },
    )
    return result.scalar_one()


class TestConcurrentDedup:
    async def test_concurrent_duplicate_insert_creates_one_row(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            await seed.commit()

        async def persist() -> int:
            async with pg_session_factory() as session:
                count = await _persist_inbound_event(
                    session,
                    {
                        "id": "wamid.duplicate",
                        "from": "919876543210",
                        "type": "text",
                        "timestamp": "1785800000",
                        "text": {"body": "hello"},
                    },
                    1,
                    "phone-1",
                )
                await session.commit()
                return count

        results = await asyncio.gather(persist(), persist())
        assert sorted(results) == [0, 1]
        async with pg_session_factory() as check:
            count = await check.scalar(select(func.count()).select_from(WhatsAppInboundEvent))
            assert count == 1


class TestClaimsAndOrdering:
    async def test_skip_locked_never_claims_same_row_twice(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            await insert_event(seed, message_id="wamid.one")
            await seed.commit()

        session_a = pg_session_factory()
        session_b = pg_session_factory()
        try:
            claim_a = await InboundEventRepository(session_a).claim_next_eligible()
            assert claim_a is not None
            claim_b = await InboundEventRepository(session_b).claim_next_eligible()
            assert claim_b is None
        finally:
            await session_a.rollback()
            await session_b.rollback()
            await session_a.close()
            await session_b.close()

    async def test_failed_head_blocks_later_same_conversation(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        now = datetime.now(UTC)
        async with pg_session_factory() as seed:
            await seed_business(seed)
            first_id = await insert_event(
                seed,
                message_id="wamid.A",
                status="failed",
                next_attempt_at=now + timedelta(hours=1),
                provider_timestamp=now,
            )
            await insert_event(
                seed,
                message_id="wamid.B",
                provider_timestamp=now + timedelta(seconds=1),
            )
            await seed.commit()

        async with pg_session_factory() as session:
            claimed = await InboundEventRepository(session).claim_next_eligible()
            assert claimed is None
            first = await session.get(WhatsAppInboundEvent, first_id)
            assert first is not None and first.status == "failed"

    async def test_different_conversations_can_claim_concurrently(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        now = datetime.now(UTC)
        async with pg_session_factory() as seed:
            await seed_business(seed)
            await insert_event(
                seed, message_id="wamid.A", sender="911111111111", provider_timestamp=now
            )
            await insert_event(
                seed,
                message_id="wamid.B",
                sender="922222222222",
                provider_timestamp=now,
            )
            await seed.commit()

        session_a = pg_session_factory()
        session_b = pg_session_factory()
        try:
            claim_a = await InboundEventRepository(session_a).claim_next_eligible()
            claim_b = await InboundEventRepository(session_b).claim_next_eligible()
            assert claim_a is not None and claim_b is not None
            assert claim_a.id != claim_b.id
        finally:
            await session_a.rollback()
            await session_b.rollback()
            await session_a.close()
            await session_b.close()


class TestClaimOwnership:
    async def test_stale_worker_cannot_overwrite_reclaimed_success(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            event_id = await insert_event(seed, message_id="wamid.lease")
            await seed.commit()

        async with pg_session_factory() as session_a:
            event_a = await InboundEventRepository(session_a).claim_next_eligible()
            assert event_a is not None and event_a.claim_token is not None
            token_a = event_a.claim_token
            version_a = event_a.claim_version
            await session_a.commit()

        async with pg_session_factory() as expire:
            await expire.execute(
                text(
                    "UPDATE whatsapp_inbound_events "
                    "SET lease_expires_at = NOW() - INTERVAL '1 second' "
                    "WHERE id = :id"
                ),
                {"id": event_id},
            )
            await expire.commit()

        async with pg_session_factory() as session_b:
            event_b = await InboundEventRepository(session_b).claim_next_eligible()
            assert event_b is not None and event_b.claim_token is not None
            await InboundEventRepository(session_b).verify_and_mark_domain_processed(
                1, event_id, event_b.claim_token, event_b.claim_version
            )
            await session_b.commit()

        async with pg_session_factory() as late_a:
            with pytest.raises(StaleClaimError):
                await InboundEventRepository(late_a).verify_and_mark_domain_processed(
                    1, event_id, token_a, version_a
                )

    async def test_cross_tenant_failure_update_rejected(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed, 1)
            await seed_business(seed, 2)
            event_id = await insert_event(seed, message_id="wamid.tenant", business_id=1)
            await seed.commit()

        async with pg_session_factory() as claim:
            event = await InboundEventRepository(claim).claim_next_eligible()
            assert event is not None and event.claim_token is not None
            token = event.claim_token
            version = event.claim_version
            await claim.commit()

        async with pg_session_factory() as wrong:
            changed = await InboundEventRepository(wrong).mark_failed(
                2, event_id, token, version, "RuntimeError"
            )
            assert changed is False
            await wrong.commit()

        async with pg_session_factory() as check:
            event = await check.get(WhatsAppInboundEvent, event_id)
            assert event is not None and event.status == "processing"


class TestAtomicOutboxAndLifecycle:
    async def test_response_enqueue_is_idempotent_and_atomic(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            event_id = await insert_event(seed, message_id="wamid.atomic")
            await seed.commit()

        async with pg_session_factory() as claim:
            event = await InboundEventRepository(claim).claim_next_eligible()
            assert event is not None and event.claim_token is not None
            claimed = ClaimedEvent(
                event_id=event.id,
                business_id=event.business_id,
                message_id=event.message_id,
                sender_phone=event.sender_phone,
                message_type=event.message_type,
                message_body=event.message_body,
                phone_number_id=event.phone_number_id,
                claim_token=event.claim_token,
                claim_version=event.claim_version,
                attempts=event.attempts,
                max_attempts=event.max_attempts,
            )
            await claim.commit()

        async with pg_session_factory() as commit:
            repo = InboundEventRepository(commit)
            await repo.acquire_conversation_lock(1, claimed.sender_phone)
            await repo.require_owned_claim(1, event_id, claimed.claim_token, claimed.claim_version)
            await _enqueue_response(claimed, "hello", commit)
            await _enqueue_response(claimed, "hello", commit)
            await repo.verify_and_mark_domain_processed(
                1, event_id, claimed.claim_token, claimed.claim_version
            )
            await commit.commit()

        async with pg_session_factory() as check:
            count = await check.scalar(
                select(func.count())
                .select_from(NotificationOutboxEvent)
                .where(NotificationOutboxEvent.entity_id == event_id)
            )
            event = await check.get(WhatsAppInboundEvent, event_id)
            assert count == 1
            assert event is not None and event.status == "domain_processed"

    async def test_rollback_removes_outbox_and_domain_processed_state(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            event_id = await insert_event(seed, message_id="wamid.rollback")
            await seed.commit()

        async with pg_session_factory() as claim:
            event = await InboundEventRepository(claim).claim_next_eligible()
            assert event is not None and event.claim_token is not None
            claimed = ClaimedEvent(
                event_id=event.id,
                business_id=event.business_id,
                message_id=event.message_id,
                sender_phone=event.sender_phone,
                message_type=event.message_type,
                message_body=event.message_body,
                phone_number_id=event.phone_number_id,
                claim_token=event.claim_token,
                claim_version=event.claim_version,
                attempts=event.attempts,
                max_attempts=event.max_attempts,
            )
            await claim.commit()

        async with pg_session_factory() as tx:
            repo = InboundEventRepository(tx)
            await _enqueue_response(claimed, "hello", tx)
            await repo.verify_and_mark_domain_processed(
                1, event_id, claimed.claim_token, claimed.claim_version
            )
            await tx.rollback()

        async with pg_session_factory() as check:
            count = await check.scalar(select(func.count()).select_from(NotificationOutboxEvent))
            event = await check.get(WhatsAppInboundEvent, event_id)
            assert count == 0
            assert event is not None and event.status == "processing"

    async def test_completion_clears_message_body(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            event_id = await insert_event(seed, message_id="wamid.complete")
            await seed.execute(
                text("UPDATE whatsapp_inbound_events SET status='domain_processed' WHERE id=:id"),
                {"id": event_id},
            )
            await seed.commit()

        async with pg_session_factory() as complete:
            await InboundEventRepository(complete).mark_completed(1, event_id, datetime.now(UTC))
            await complete.commit()

        async with pg_session_factory() as check:
            event = await check.get(WhatsAppInboundEvent, event_id)
            assert event is not None
            assert event.status == "completed"
            assert event.message_body is None
            assert event.completed_at is not None


class TestLiveConstraintsAndRetention:
    @pytest.mark.parametrize("status", ["bogus", "sent", "done"])
    async def test_invalid_status_rejected(
        self, pg_session_factory: async_sessionmaker[AsyncSession], status: str
    ) -> None:
        async with pg_session_factory() as session:
            await seed_business(session)
            with pytest.raises(IntegrityError):
                await insert_event(session, message_id="wamid.invalid", status=status)
                await session.flush()

    @pytest.mark.parametrize(
        "attempts,max_attempts",
        [(-1, 5), (0, 0), (6, 5)],
    )
    async def test_invalid_attempt_counts_rejected(
        self,
        pg_session_factory: async_sessionmaker[AsyncSession],
        attempts: int,
        max_attempts: int,
    ) -> None:
        async with pg_session_factory() as session:
            await seed_business(session)
            with pytest.raises(IntegrityError):
                await insert_event(
                    session,
                    message_id="wamid.invalid",
                    attempts=attempts,
                    max_attempts=max_attempts,
                )
                await session.flush()

    async def test_completed_requires_timestamp_and_cleared_body(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as session:
            await seed_business(session)
            with pytest.raises(IntegrityError):
                await insert_event(session, message_id="wamid.badcomplete", status="completed")
                await session.flush()

    async def test_dead_letter_retention_cleanup(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=31)
        async with pg_session_factory() as seed:
            await seed_business(seed)
            event_id = await insert_event(
                seed,
                message_id="wamid.dead",
                attempts=0,
                max_attempts=5,
            )
            await seed.execute(
                text(
                    "UPDATE whatsapp_inbound_events "
                    "SET status='dead_letter', attempts=5, dead_lettered_at=:old "
                    "WHERE id=:id"
                ),
                {"old": old, "id": event_id},
            )
            await seed.commit()

        async with pg_session_factory() as cleanup:
            result = await DataRetentionService(cleanup).run_cleanup()
            await cleanup.commit()
            assert result.inbound_events_deleted == 1

        async with pg_session_factory() as check:
            assert await check.get(WhatsAppInboundEvent, event_id) is None


class TestDeliveryReconciliation:
    async def test_provider_status_completes_inbound_and_clears_body(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            event_id = await insert_event(seed, message_id="wamid.status")
            await seed.execute(
                text("UPDATE whatsapp_inbound_events SET status='domain_processed' WHERE id=:id"),
                {"id": event_id},
            )
            outbox = await NotificationRepository(seed).insert_event_idempotent(
                {
                    "business_id": 1,
                    "event_type": "whatsapp_inbound_response",
                    "entity_type": "whatsapp_inbound_event",
                    "entity_id": event_id,
                    "recipient_type": "patient",
                    "recipient_phone": "919876543210",
                    "channel": "whatsapp",
                    "payload": {
                        "response_text": "ok",
                        "phone_number_id": "phone-1",
                    },
                    "status": "unknown",
                    "idempotency_key": "whatsapp-response-wamid.status",
                }
            )
            assert outbox is not None
            seed.add(
                WhatsAppDeliveryAttempt(
                    business_id=1,
                    notification_event_id=outbox.id,
                    attempt_number=1,
                    status="accepted",
                    provider_message_id="meta-1",
                )
            )
            await seed.commit()

        async with pg_session_factory() as status_session:
            changed = await _persist_delivery_status(
                status_session,
                {"id": "meta-1", "status": "delivered"},
                1,
            )
            assert changed == 1
            await status_session.commit()

        async with pg_session_factory() as check:
            event = await check.get(WhatsAppInboundEvent, event_id)
            assert event is not None
            assert event.status == "completed"
            assert event.message_body is None

    async def test_duplicate_read_and_late_failed_status_do_not_regress_delivery(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            event_id = await insert_event(seed, message_id="wamid.monotonic")
            await seed.execute(
                text("UPDATE whatsapp_inbound_events SET status='domain_processed' WHERE id=:id"),
                {"id": event_id},
            )
            outbox = await NotificationRepository(seed).insert_event_idempotent(
                {
                    "business_id": 1,
                    "event_type": "whatsapp_inbound_response",
                    "entity_type": "whatsapp_inbound_event",
                    "entity_id": event_id,
                    "recipient_type": "patient",
                    "recipient_phone": "919876543210",
                    "channel": "whatsapp",
                    "payload": {"response_text": "ok", "phone_number_id": "phone-1"},
                    "status": "unknown",
                    "idempotency_key": "whatsapp-response-wamid.monotonic",
                }
            )
            assert outbox is not None
            seed.add(
                WhatsAppDeliveryAttempt(
                    business_id=1,
                    notification_event_id=outbox.id,
                    attempt_number=1,
                    status="accepted",
                    provider_message_id="meta-monotonic",
                )
            )
            await seed.commit()

        for provider_status in ("delivered", "read", "failed"):
            async with pg_session_factory() as callback:
                changed = await _persist_delivery_status(
                    callback,
                    {"id": "meta-monotonic", "status": provider_status},
                    1,
                )
                assert changed == 1
                await callback.commit()

        async with pg_session_factory() as check:
            outbox_row = await check.get(NotificationOutboxEvent, outbox.id)
            inbound = await check.get(WhatsAppInboundEvent, event_id)
            assert outbox_row is not None and outbox_row.status == "delivered"
            assert inbound is not None and inbound.status == "completed"

    async def test_final_attempt_provider_failure_dead_letters_and_unblocks_inbound(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            event_id = await insert_event(seed, message_id="wamid.finalfailed")
            await seed.execute(
                text("UPDATE whatsapp_inbound_events SET status='domain_processed' WHERE id=:id"),
                {"id": event_id},
            )
            outbox = await NotificationRepository(seed).insert_event_idempotent(
                {
                    "business_id": 1,
                    "event_type": "whatsapp_inbound_response",
                    "entity_type": "whatsapp_inbound_event",
                    "entity_id": event_id,
                    "recipient_type": "patient",
                    "recipient_phone": "919876543210",
                    "channel": "whatsapp",
                    "payload": {"response_text": "ok", "phone_number_id": "phone-1"},
                    "status": "unknown",
                    "attempts": 5,
                    "max_attempts": 5,
                    "idempotency_key": "whatsapp-response-wamid.finalfailed",
                }
            )
            assert outbox is not None
            seed.add(
                WhatsAppDeliveryAttempt(
                    business_id=1,
                    notification_event_id=outbox.id,
                    attempt_number=5,
                    status="accepted",
                    provider_message_id="meta-finalfailed",
                )
            )
            await seed.commit()

        async with pg_session_factory() as callback:
            await _persist_delivery_status(
                callback,
                {"id": "meta-finalfailed", "status": "failed"},
                1,
            )
            await callback.commit()

        async with pg_session_factory() as check:
            outbox_row = await check.get(NotificationOutboxEvent, outbox.id)
            inbound = await check.get(WhatsAppInboundEvent, event_id)
            assert outbox_row is not None and outbox_row.status == "dead_letter"
            assert inbound is not None and inbound.status == "response_failed"

    async def test_terminal_fallback_delivery_completes_dead_letter(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            event_id = await insert_event(seed, message_id="wamid.fallback")
            await seed.execute(
                text(
                    "UPDATE whatsapp_inbound_events SET status='dead_letter', "
                    "attempts=5, dead_lettered_at=NOW() WHERE id=:id"
                ),
                {"id": event_id},
            )
            outbox = await NotificationRepository(seed).insert_event_idempotent(
                {
                    "business_id": 1,
                    "event_type": "whatsapp_inbound_response",
                    "entity_type": "whatsapp_inbound_event",
                    "entity_id": event_id,
                    "recipient_type": "patient",
                    "recipient_phone": "919876543210",
                    "channel": "whatsapp",
                    "payload": {
                        "response_text": "fallback",
                        "phone_number_id": "phone-1",
                        "terminal_fallback": True,
                    },
                    "status": "unknown",
                    "idempotency_key": "whatsapp-response-wamid.fallback",
                }
            )
            assert outbox is not None
            seed.add(
                WhatsAppDeliveryAttempt(
                    business_id=1,
                    notification_event_id=outbox.id,
                    attempt_number=1,
                    status="accepted",
                    provider_message_id="meta-fallback",
                )
            )
            await seed.commit()

        async with pg_session_factory() as session:
            await _persist_delivery_status(
                session,
                {"id": "meta-fallback", "status": "delivered"},
                1,
            )
            await session.commit()

        async with pg_session_factory() as check:
            event = await check.get(WhatsAppInboundEvent, event_id)
            assert event is not None and event.status == "completed"
            assert event.message_body is None

    async def test_stale_processing_notification_is_reclaimed(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            outbox = await NotificationRepository(seed).insert_event_idempotent(
                {
                    "business_id": 1,
                    "event_type": "appointment_confirmed",
                    "entity_type": "appointment",
                    "entity_id": 1,
                    "recipient_type": "patient",
                    "recipient_phone": "919876543210",
                    "channel": "whatsapp",
                    "payload": {"phone_number_id": "phone-1"},
                    "status": "pending",
                    "idempotency_key": "stale-notification",
                }
            )
            assert outbox is not None
            await seed.execute(
                text(
                    "UPDATE notification_outbox SET status='processing', "
                    "claim_token=:token, claim_version=2, "
                    "lease_expires_at=NOW()-INTERVAL '1 minute', "
                    "updated_at=NOW()-INTERVAL '6 minutes' WHERE id=:id"
                ),
                {"id": outbox.id, "token": uuid.uuid4()},
            )
            await seed.commit()

        async with pg_session_factory() as claim:
            claimed = await NotificationRepository(claim).claim_pending_events(limit=1)
            assert len(claimed) == 1
            assert claimed[0].id == outbox.id

    async def test_stale_notification_worker_cannot_finalize_reclaimed_delivery(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            outbox = await NotificationRepository(seed).insert_event_idempotent(
                {
                    "business_id": 1,
                    "event_type": "appointment_confirmed",
                    "entity_type": "appointment",
                    "entity_id": 1,
                    "recipient_type": "patient",
                    "recipient_phone": "919876543210",
                    "channel": "whatsapp",
                    "payload": {"phone_number_id": "phone-1"},
                    "status": "pending",
                    "idempotency_key": "notification-race",
                }
            )
            assert outbox is not None
            await seed.commit()

        claim_a = await _claim_one(pg_session_factory)
        assert claim_a is not None
        async with pg_session_factory() as expire:
            await expire.execute(
                text(
                    "UPDATE notification_outbox SET "
                    "lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=:id"
                ),
                {"id": outbox.id},
            )
            await expire.commit()
        claim_b = await _claim_one(pg_session_factory)
        assert claim_b is not None
        assert claim_b.claim_token != claim_a.claim_token

        with pytest.raises(RuntimeError, match="notification_claim_stale"):
            await _record_accepted(
                pg_session_factory,
                claim_a,
                1,
                DeliveryReceipt(provider_message_id="meta-stale", final=True),
            )

    async def test_reclaimed_sending_attempt_is_not_sent_twice(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as seed:
            await seed_business(seed)
            outbox = await NotificationRepository(seed).insert_event_idempotent(
                {
                    "business_id": 1,
                    "event_type": "appointment_confirmed",
                    "entity_type": "appointment",
                    "entity_id": 1,
                    "recipient_type": "patient",
                    "recipient_phone": "919876543210",
                    "channel": "whatsapp",
                    "payload": {"phone_number_id": "phone-1"},
                    "status": "pending",
                    "idempotency_key": "crash-window",
                }
            )
            assert outbox is not None
            await seed.commit()

        first_claim = await _claim_one(pg_session_factory)
        assert first_claim is not None
        async with pg_session_factory() as attempt_session:
            attempt_session.add(
                WhatsAppDeliveryAttempt(
                    business_id=1,
                    notification_event_id=outbox.id,
                    attempt_number=1,
                    status="sending",
                )
            )
            await attempt_session.commit()
        async with pg_session_factory() as expire:
            await expire.execute(
                text(
                    "UPDATE notification_outbox SET "
                    "lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=:id"
                ),
                {"id": outbox.id},
            )
            await expire.commit()

        reclaimed = await _claim_one(pg_session_factory)
        assert reclaimed is not None
        sender = AsyncMock()
        await _deliver_claimed(pg_session_factory, sender, reclaimed)
        sender.send.assert_not_awaited()

        async with pg_session_factory() as check:
            row = await check.get(NotificationOutboxEvent, outbox.id)
            assert row is not None and row.status == "unknown"

    async def test_response_failed_retention_cleanup(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=31)
        async with pg_session_factory() as seed:
            await seed_business(seed)
            event_id = await insert_event(seed, message_id="wamid.responsefailed")
            await seed.execute(
                text(
                    "UPDATE whatsapp_inbound_events SET status='response_failed', "
                    "dead_lettered_at=:old WHERE id=:id"
                ),
                {"old": old, "id": event_id},
            )
            await seed.commit()

        async with pg_session_factory() as cleanup:
            result = await DataRetentionService(cleanup).run_cleanup()
            await cleanup.commit()
            assert result.inbound_events_deleted == 1
