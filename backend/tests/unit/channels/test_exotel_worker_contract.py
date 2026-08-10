"""Worker contract tests — claim, complete, fail, dead-letter, stale claim.

Uses InMemoryCallEventIntake with claim/complete/fail lifecycle to prove
the worker contract independently of PostgreSQL.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fonely.api.channels.exotel import router
from fonely.core.config import settings
from fonely.domain.calls.events import parse_exotel_callback
from fonely.domain.calls.intake import ClaimedCallEvent, InboundCallEvent
from fonely.services.exotel_config import ExotelNumberMapping
from fonely.workers.exotel_worker import SchemaNotReadyError
from tests.fixtures.exotel_callbacks.fixtures import (
    ANSWERED_OUTBOUND,
    COMPLETED_OUTBOUND,
    FAILED_OUTBOUND,
)
from tests.fixtures.exotel_callbacks.test_intake import InMemoryCallEventIntake

_TEST_SECRET = "test-exotel-webhook-secret-value"


def _create_app() -> tuple[FastAPI, InMemoryCallEventIntake]:
    app = FastAPI()
    app.include_router(router)
    app.state.exotel_mapping = ExotelNumberMapping({"08012345678": 1})
    intake = InMemoryCallEventIntake()
    app.state.exotel_intake = intake
    return app, intake


def _to_inbound(fixture: dict) -> InboundCallEvent:
    """Parse fixture via Exotel DTO then map to neutral event."""
    return parse_exotel_callback(fixture).to_inbound_event()


@pytest.fixture(autouse=True)
def _configure_secret():
    with patch.object(settings, "exotel_webhook_secret", _TEST_SECRET):
        yield


def _auth_headers() -> dict[str, str]:
    return {"X-Exotel-Webhook-Secret": _TEST_SECRET}


async def _complete(intake: InMemoryCallEventIntake, c: ClaimedCallEvent) -> bool:
    return await intake.mark_completed(
        c.id,
        c.business_id,
        c.claim_token,
        c.claim_version,
    )


async def _fail(intake: InMemoryCallEventIntake, c: ClaimedCallEvent) -> bool:
    return await intake.mark_failed(
        c.id,
        c.business_id,
        c.claim_token,
        c.claim_version,
    )


# ============================================================================
# Worker contract: claim lifecycle
# ============================================================================


class TestClaimLifecycle:
    async def test_persist_creates_received_event(self) -> None:
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        record = await intake.persist(1, event)
        assert intake.get_intake_status(record.id) == "received"

    async def test_claim_transitions_to_processing(self) -> None:
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        assert claimed.provider_call_id == event.provider_call_id
        assert intake.get_intake_status(1) == "processing"

    async def test_claim_empty_queue_returns_none(self) -> None:
        intake = InMemoryCallEventIntake()
        assert await intake.claim_next_eligible() is None

    async def test_claim_skips_processing_events(self) -> None:
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        await intake.claim_next_eligible()
        assert await intake.claim_next_eligible() is None

    async def test_complete_transitions_to_completed(self) -> None:
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await _complete(intake, claimed)
        assert ok
        assert intake.get_intake_status(1) == "completed"

    async def test_fail_transitions_to_failed(self) -> None:
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await _fail(intake, claimed)
        assert ok
        assert intake.get_intake_status(1) == "failed"

    async def test_failed_event_re_claimable(self) -> None:
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        await _fail(intake, claimed)
        reclaimed = await intake.claim_next_eligible()
        assert reclaimed is not None
        assert reclaimed.id == claimed.id

    async def test_stale_claim_token_rejected(self) -> None:
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await intake.mark_completed(
            claimed.id,
            claimed.business_id,
            "wrong-token",
            claimed.claim_version,
        )
        assert not ok
        assert intake.get_intake_status(1) == "processing"

    async def test_stale_claim_version_rejected(self) -> None:
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await intake.mark_completed(
            claimed.id,
            claimed.business_id,
            claimed.claim_token,
            999,
        )
        assert not ok

    async def test_dead_letter_after_max_attempts(self) -> None:
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        record = await intake.persist(1, event)
        for _ in range(5):
            claimed = await intake.claim_next_eligible()
            if claimed is None:
                break
            await _fail(intake, claimed)
        assert intake.get_intake_status(record.id) == "dead_letter"

    async def test_dead_letter_not_reclaimable(self) -> None:
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        for _ in range(5):
            claimed = await intake.claim_next_eligible()
            if claimed is None:
                break
            await _fail(intake, claimed)
        assert await intake.claim_next_eligible() is None


# ============================================================================
# Schema guard
# ============================================================================


class TestSchemaGuard:
    async def test_verify_schema_raises_without_column(self) -> None:
        """Worker._verify_schema raises when COUNT(*)=0 for
        provider_call_id in current_schema()."""
        from unittest.mock import AsyncMock, MagicMock

        from fonely.workers.exotel_worker import InboundCallEventWorker

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        worker = InboundCallEventWorker(AsyncMock())
        with pytest.raises(SchemaNotReadyError, match="provider_call_id"):
            await worker._verify_schema(mock_session)

    async def test_verify_schema_passes_with_column(self) -> None:
        """Worker._verify_schema succeeds when COUNT(*)>0."""
        from unittest.mock import AsyncMock, MagicMock

        from fonely.workers.exotel_worker import InboundCallEventWorker

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 1
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        worker = InboundCallEventWorker(AsyncMock())
        await worker._verify_schema(mock_session)
        assert worker._schema_verified is True

    async def test_verify_schema_caches_after_first_success(self) -> None:
        """Schema check is cached — second call skips DB query."""
        from unittest.mock import AsyncMock, MagicMock

        from fonely.workers.exotel_worker import InboundCallEventWorker

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 1
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        worker = InboundCallEventWorker(AsyncMock())
        await worker._verify_schema(mock_session)
        mock_session.execute.reset_mock()
        await worker._verify_schema(mock_session)
        mock_session.execute.assert_not_called()


# ============================================================================
# Adapter → intake → worker functional proof
# ============================================================================


class TestAdapterIntakeWorkerProof:
    async def test_full_vertical_answered_then_completed(self) -> None:
        """Adapter persists → worker claims and completes both events."""
        app, intake = _create_app()
        client = TestClient(app)

        client.post("/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers())
        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )

        assert len(intake.events) == 2

        claimed1 = await intake.claim_next_eligible()
        assert claimed1 is not None
        assert claimed1.status == "in_progress"
        await _complete(intake, claimed1)

        claimed2 = await intake.claim_next_eligible()
        assert claimed2 is not None
        assert claimed2.status == "completed"
        await _complete(intake, claimed2)

        assert await intake.claim_next_eligible() is None
        assert intake.get_intake_status(1) == "completed"
        assert intake.get_intake_status(2) == "completed"

    async def test_full_vertical_failed_call(self) -> None:
        """Adapter persists failed → worker claims and completes."""
        app, intake = _create_app()
        client = TestClient(app)

        client.post("/webhooks/exotel/call-status", json=FAILED_OUTBOUND, headers=_auth_headers())

        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        assert claimed.status == "failed"
        await _complete(intake, claimed)
        assert intake.get_intake_status(1) == "completed"

    async def test_full_vertical_worker_failure_retries(self) -> None:
        """Worker fails → event goes to failed → re-claimable."""
        app, intake = _create_app()
        client = TestClient(app)

        client.post("/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers())

        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        await _fail(intake, claimed)
        assert intake.get_intake_status(1) == "failed"
        assert intake.get_attempts(1) == 1

        reclaimed = await intake.claim_next_eligible()
        assert reclaimed is not None
        assert reclaimed.id == claimed.id
        await _complete(intake, reclaimed)
        assert intake.get_intake_status(1) == "completed"

    async def test_full_vertical_duplicate_callbacks_single_event(self) -> None:
        """Duplicate adapter callbacks produce one intake event, one worker claim."""
        app, intake = _create_app()
        client = TestClient(app)

        r1 = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers()
        )
        r2 = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers()
        )
        assert r1.status_code == 200
        assert r2.status_code == 200

        assert len(intake.events) == 1

        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        await _complete(intake, claimed)
        assert await intake.claim_next_eligible() is None

    async def test_worker_ooo_late_event_completes_as_noop(self) -> None:
        """Late answered event after terminal: intake stores both; worker
        completes the late event as a no-op (no domain mutation crash)."""
        app, intake = _create_app()
        client = TestClient(app)

        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        late_answered = {**ANSWERED_OUTBOUND, "CallSid": COMPLETED_OUTBOUND["CallSid"]}
        client.post("/webhooks/exotel/call-status", json=late_answered, headers=_auth_headers())
        assert len(intake.events) == 2

        # First claim: terminal — normal processing
        c1 = await intake.claim_next_eligible()
        assert c1 is not None
        assert c1.status == "completed"
        await _complete(intake, c1)

        # Second claim: late answered — worker would catch LateCallEventError
        # and still mark_completed (no-op domain mutation)
        c2 = await intake.claim_next_eligible()
        assert c2 is not None
        assert c2.event_type == "answered"
        assert c2.status == "in_progress"
        await _complete(intake, c2)
        assert intake.get_intake_status(c2.id) == "completed"

    async def test_late_event_persisted_and_claimable(self) -> None:
        """Late lower-state event is persisted by intake (worker handles no-op)."""
        app, intake = _create_app()
        client = TestClient(app)

        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        late_answered = {**ANSWERED_OUTBOUND, "CallSid": COMPLETED_OUTBOUND["CallSid"]}
        r2 = client.post(
            "/webhooks/exotel/call-status", json=late_answered, headers=_auth_headers()
        )
        assert r2.status_code == 200
        assert len(intake.events) == 2

        claimed1 = await intake.claim_next_eligible()
        assert claimed1 is not None
        await _complete(intake, claimed1)

        claimed2 = await intake.claim_next_eligible()
        assert claimed2 is not None
        assert claimed2.event_type == "answered"
        await _complete(intake, claimed2)


# ============================================================================
# Tenant isolation
# ============================================================================


class TestTenantIsolation:
    async def test_mark_completed_rejects_wrong_business_id(self) -> None:
        """mark_completed must reject if business_id doesn't match."""
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await intake.mark_completed(
            claimed.id,
            999,
            claimed.claim_token,
            claimed.claim_version,
        )
        assert not ok
        assert intake.get_intake_status(1) == "processing"

    async def test_mark_failed_rejects_wrong_business_id(self) -> None:
        """mark_failed must reject if business_id doesn't match."""
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await intake.mark_failed(
            claimed.id,
            999,
            claimed.claim_token,
            claimed.claim_version,
        )
        assert not ok
        assert intake.get_intake_status(1) == "processing"

    async def test_different_tenants_independent_events(self) -> None:
        """Events for different business_ids are fully independent."""
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        r1 = await intake.persist(1, event)
        r2 = await intake.persist(2, event)
        assert r1.business_id == 1
        assert r2.business_id == 2
        assert r1.id != r2.id


# ============================================================================
# SQL contract patterns (provable without PG, demonstrating the SQL shapes)
# ============================================================================


class TestSQLContractPatterns:
    async def test_on_conflict_duplicate_detection(self) -> None:
        """Same dedup key, same digest → DuplicateCallEventError."""
        intake = InMemoryCallEventIntake()
        event = _to_inbound(COMPLETED_OUTBOUND)
        await intake.persist(1, event)
        from fonely.domain.calls.intake import DuplicateCallEventError

        with pytest.raises(DuplicateCallEventError):
            await intake.persist(1, event)

    async def test_on_conflict_conflict_detection(self) -> None:
        """Same dedup key, different digest → ConflictingCallEventError."""
        intake = InMemoryCallEventIntake()
        event = _to_inbound(COMPLETED_OUTBOUND)
        await intake.persist(1, event)
        modified = InboundCallEvent(
            provider=event.provider,
            provider_call_id=event.provider_call_id,
            event_type=event.event_type,
            status=event.status,
            caller_phone=event.caller_phone,
            called_number=event.called_number,
            duration=999,
            conversation_duration=None,
            direction=event.direction,
            custom_field=event.custom_field,
        )
        from fonely.domain.calls.intake import ConflictingCallEventError

        with pytest.raises(ConflictingCallEventError):
            await intake.persist(1, modified)

    async def test_claim_fence_stale_token(self) -> None:
        """Stale claim_token must be rejected by mark_completed."""
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await intake.mark_completed(
            claimed.id, claimed.business_id, "stale-token", claimed.claim_version
        )
        assert not ok

    async def test_claim_fence_stale_version(self) -> None:
        """Stale claim_version must be rejected by mark_completed."""
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await intake.mark_completed(
            claimed.id, claimed.business_id, claimed.claim_token, claimed.claim_version + 100
        )
        assert not ok

    async def test_backoff_retry_cycle(self) -> None:
        """Failed events are re-claimable; attempt count increments."""
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        record = await intake.persist(1, event)
        for i in range(3):
            claimed = await intake.claim_next_eligible()
            assert claimed is not None
            assert intake.get_attempts(record.id) == i + 1
            await intake.mark_failed(
                claimed.id, claimed.business_id, claimed.claim_token, claimed.claim_version
            )
            assert intake.get_intake_status(record.id) == "failed"

    async def test_max_attempts_dead_letter(self) -> None:
        """After max_attempts (5), event goes to dead_letter, not failed."""
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        record = await intake.persist(1, event)
        for i in range(5):
            claimed = await intake.claim_next_eligible()
            assert claimed is not None, f"should be claimable on attempt {i + 1}"
            await intake.mark_failed(
                claimed.id, claimed.business_id, claimed.claim_token, claimed.claim_version
            )
        assert intake.get_intake_status(record.id) == "dead_letter"
        assert await intake.claim_next_eligible() is None

    async def test_completed_event_not_reclaimable(self) -> None:
        """Once completed, an event cannot be claimed again."""
        intake = InMemoryCallEventIntake()
        event = _to_inbound(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        await intake.mark_completed(
            claimed.id, claimed.business_id, claimed.claim_token, claimed.claim_version
        )
        assert await intake.claim_next_eligible() is None
