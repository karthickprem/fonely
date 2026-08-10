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
from fonely.services.exotel_config import ExotelNumberMapping
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


@pytest.fixture(autouse=True)
def _configure_secret():
    with patch.object(settings, "exotel_webhook_secret", _TEST_SECRET):
        yield


def _auth_headers() -> dict[str, str]:
    return {"X-Exotel-Webhook-Secret": _TEST_SECRET}


# ============================================================================
# Worker contract: claim lifecycle
# ============================================================================


class TestClaimLifecycle:
    async def test_persist_creates_received_event(self) -> None:
        intake = InMemoryCallEventIntake()
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        record = await intake.persist(1, event)
        assert intake.get_intake_status(record.id) == "received"

    async def test_claim_transitions_to_processing(self) -> None:
        intake = InMemoryCallEventIntake()
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        assert claimed.call_sid == event.call_sid
        assert intake.get_intake_status(1) == "processing"

    async def test_claim_empty_queue_returns_none(self) -> None:
        intake = InMemoryCallEventIntake()
        assert await intake.claim_next_eligible() is None

    async def test_claim_skips_processing_events(self) -> None:
        intake = InMemoryCallEventIntake()
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        await intake.claim_next_eligible()
        assert await intake.claim_next_eligible() is None

    async def test_complete_transitions_to_completed(self) -> None:
        intake = InMemoryCallEventIntake()
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await intake.mark_completed(claimed.id, claimed.claim_token, claimed.claim_version)
        assert ok
        assert intake.get_intake_status(1) == "completed"

    async def test_fail_transitions_to_failed(self) -> None:
        intake = InMemoryCallEventIntake()
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await intake.mark_failed(claimed.id, claimed.claim_token, claimed.claim_version)
        assert ok
        assert intake.get_intake_status(1) == "failed"

    async def test_failed_event_re_claimable(self) -> None:
        intake = InMemoryCallEventIntake()
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        await intake.mark_failed(claimed.id, claimed.claim_token, claimed.claim_version)
        reclaimed = await intake.claim_next_eligible()
        assert reclaimed is not None
        assert reclaimed.id == claimed.id

    async def test_stale_claim_token_rejected(self) -> None:
        intake = InMemoryCallEventIntake()
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await intake.mark_completed(claimed.id, "wrong-token", claimed.claim_version)
        assert not ok
        assert intake.get_intake_status(1) == "processing"

    async def test_stale_claim_version_rejected(self) -> None:
        intake = InMemoryCallEventIntake()
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        ok = await intake.mark_completed(claimed.id, claimed.claim_token, 999)
        assert not ok

    async def test_dead_letter_after_max_attempts(self) -> None:
        intake = InMemoryCallEventIntake()
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        record = await intake.persist(1, event)
        for _ in range(5):
            claimed = await intake.claim_next_eligible()
            if claimed is None:
                break
            await intake.mark_failed(claimed.id, claimed.claim_token, claimed.claim_version)
        assert intake.get_intake_status(record.id) == "dead_letter"

    async def test_dead_letter_not_reclaimable(self) -> None:
        intake = InMemoryCallEventIntake()
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        await intake.persist(1, event)
        for _ in range(5):
            claimed = await intake.claim_next_eligible()
            if claimed is None:
                break
            await intake.mark_failed(claimed.id, claimed.claim_token, claimed.claim_version)
        assert await intake.claim_next_eligible() is None


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
        assert claimed1.status == "in-progress"
        await intake.mark_completed(claimed1.id, claimed1.claim_token, claimed1.claim_version)

        claimed2 = await intake.claim_next_eligible()
        assert claimed2 is not None
        assert claimed2.status == "completed"
        await intake.mark_completed(claimed2.id, claimed2.claim_token, claimed2.claim_version)

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
        await intake.mark_completed(claimed.id, claimed.claim_token, claimed.claim_version)
        assert intake.get_intake_status(1) == "completed"

    async def test_full_vertical_worker_failure_retries(self) -> None:
        """Worker fails → event goes to failed → re-claimable."""
        app, intake = _create_app()
        client = TestClient(app)

        client.post("/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers())

        claimed = await intake.claim_next_eligible()
        assert claimed is not None
        await intake.mark_failed(claimed.id, claimed.claim_token, claimed.claim_version)
        assert intake.get_intake_status(1) == "failed"
        assert intake.get_attempts(1) == 1

        reclaimed = await intake.claim_next_eligible()
        assert reclaimed is not None
        assert reclaimed.id == claimed.id
        await intake.mark_completed(reclaimed.id, reclaimed.claim_token, reclaimed.claim_version)
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
        await intake.mark_completed(claimed.id, claimed.claim_token, claimed.claim_version)
        assert await intake.claim_next_eligible() is None
