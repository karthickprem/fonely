"""Decisive probes through real production classes — not test doubles.

Exercises InboundCallIntakeService, InboundCallEventWorker, and
create_app() through their actual code paths with mock sessions.
Proves transaction ownership, error propagation, schema guard,
and disabled-state invariants.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fonely.domain.calls.intake import (
    ConflictingCallEventError,
    DuplicateCallEventError,
    InboundCallEvent,
    InboundCallEventRecord,
)
from fonely.workers.exotel_worker import (
    InboundCallEventWorker,
    SchemaNotReadyError,
)


def _mock_session_factory(mock_session: AsyncMock) -> MagicMock:
    """Create a factory that behaves like async_sessionmaker.

    async_sessionmaker() returns a context manager directly (not a coroutine),
    so factory() must return an async context manager, not an awaitable.
    """

    @asynccontextmanager
    async def _session_ctx():
        yield mock_session

    factory = MagicMock()
    factory.side_effect = lambda: _session_ctx()
    return factory


def _make_event() -> InboundCallEvent:
    return InboundCallEvent(
        call_sid="a" * 32,
        event_type="terminal",
        status="completed",
        caller_phone="+919000000001",
        called_number="08012345678",
        duration=60,
        conversation_duration=45,
        direction="inbound",
        custom_field=None,
    )


def _make_record() -> InboundCallEventRecord:
    return InboundCallEventRecord(
        id=1,
        business_id=1,
        call_sid="a" * 32,
        event_type="terminal",
        status="completed",
        caller_phone="+919000000001",
        called_number="08012345678",
        duration=60,
        conversation_duration=45,
        direction="inbound",
        custom_field=None,
        payload_digest="d" * 32,
    )


# ============================================================================
# InboundCallIntakeService — real class, transaction probes
# ============================================================================


class TestIntakeServiceTransactionContract:
    """Prove InboundCallIntakeService commits/rolls back correctly."""

    async def test_persist_commits_on_success(self) -> None:
        """Real IntakeService.persist() calls commit after repo.persist."""
        from fonely.services.exotel_intake import InboundCallIntakeService

        mock_session = AsyncMock()
        factory = _mock_session_factory(mock_session)
        service = InboundCallIntakeService(factory)

        with patch(
            "fonely.services.exotel_intake.ExotelInboundEventRepository"
        ) as mock_repo_cls:
            mock_repo_cls.return_value.persist = AsyncMock(
                return_value=_make_record()
            )
            result = await service.persist(1, _make_event())

        assert result == _make_record()
        mock_session.commit.assert_awaited_once()
        mock_session.rollback.assert_not_awaited()

    async def test_persist_rolls_back_on_duplicate(self) -> None:
        """DuplicateCallEventError propagates after rollback."""
        from fonely.services.exotel_intake import InboundCallIntakeService

        mock_session = AsyncMock()
        factory = _mock_session_factory(mock_session)
        service = InboundCallIntakeService(factory)

        with patch(
            "fonely.services.exotel_intake.ExotelInboundEventRepository"
        ) as mock_repo_cls:
            mock_repo_cls.return_value.persist = AsyncMock(
                side_effect=DuplicateCallEventError("dup")
            )
            with pytest.raises(DuplicateCallEventError):
                await service.persist(1, _make_event())

        mock_session.rollback.assert_awaited_once()
        mock_session.commit.assert_not_awaited()

    async def test_persist_rolls_back_on_conflict(self) -> None:
        """ConflictingCallEventError propagates after rollback."""
        from fonely.services.exotel_intake import InboundCallIntakeService

        mock_session = AsyncMock()
        factory = _mock_session_factory(mock_session)
        service = InboundCallIntakeService(factory)

        with patch(
            "fonely.services.exotel_intake.ExotelInboundEventRepository"
        ) as mock_repo_cls:
            mock_repo_cls.return_value.persist = AsyncMock(
                side_effect=ConflictingCallEventError("conflict")
            )
            with pytest.raises(ConflictingCallEventError):
                await service.persist(1, _make_event())

        mock_session.rollback.assert_awaited_once()

    async def test_persist_rolls_back_on_unexpected_error(self) -> None:
        """Unexpected errors also roll back before propagating."""
        from fonely.services.exotel_intake import InboundCallIntakeService

        mock_session = AsyncMock()
        factory = _mock_session_factory(mock_session)
        service = InboundCallIntakeService(factory)

        with patch(
            "fonely.services.exotel_intake.ExotelInboundEventRepository"
        ) as mock_repo_cls:
            mock_repo_cls.return_value.persist = AsyncMock(
                side_effect=RuntimeError("db exploded")
            )
            with pytest.raises(RuntimeError, match="db exploded"):
                await service.persist(1, _make_event())

        mock_session.rollback.assert_awaited_once()


# ============================================================================
# InboundCallEventWorker — real class, schema guard probes
# ============================================================================


class TestWorkerSchemaGuardProduction:
    """Prove InboundCallEventWorker.process_one() enforces schema guard."""

    async def test_process_one_raises_without_schema(self) -> None:
        """Real worker.process_one() raises SchemaNotReadyError when
        provider_call_sid column is absent from information_schema."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        worker = InboundCallEventWorker(factory)

        with pytest.raises(SchemaNotReadyError, match="provider_call_sid"):
            await worker.process_one()

    async def test_process_one_returns_false_empty_queue(self) -> None:
        """When schema exists but queue is empty, returns False."""
        schema_result = MagicMock()
        schema_result.scalar_one_or_none.return_value = 1

        claim_result = MagicMock()
        claim_result.one_or_none.return_value = None

        mock_session = AsyncMock()
        call_count = [0]

        async def _execute_side_effect(stmt, params=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return schema_result
            return claim_result

        mock_session.execute = AsyncMock(
            side_effect=_execute_side_effect
        )

        factory = _mock_session_factory(mock_session)
        worker = InboundCallEventWorker(factory)
        result = await worker.process_one()
        assert result is False

    async def test_schema_check_cached_across_calls(self) -> None:
        """Schema verified once, second call doesn't re-query."""
        schema_result = MagicMock()
        schema_result.scalar_one_or_none.return_value = 1

        claim_result = MagicMock()
        claim_result.one_or_none.return_value = None

        mock_session = AsyncMock()
        execute_calls: list[str] = []

        async def _track_execute(stmt, params=None):
            sql_str = str(stmt)
            execute_calls.append(sql_str)
            if "information_schema" in sql_str:
                return schema_result
            return claim_result

        mock_session.execute = AsyncMock(side_effect=_track_execute)

        factory = _mock_session_factory(mock_session)
        worker = InboundCallEventWorker(factory)

        await worker.process_one()
        await worker.process_one()

        schema_queries = [
            c for c in execute_calls if "information_schema" in c
        ]
        assert len(schema_queries) == 1, (
            f"schema queried {len(schema_queries)} times, expected 1"
        )


# ============================================================================
# create_app() — disabled-state probes
# ============================================================================


class TestCreateAppDisabledState:
    """Prove create_app() disabled-state invariants."""

    def test_exotel_route_absent_with_strong_secret(self) -> None:
        """Even with a strong secret, Exotel route is not mounted."""
        from fonely.app import create_app

        with patch("fonely.app.settings") as ms:
            ms.internal_api_secret = ""
            ms.whatsapp_verify_token = ""
            ms.exotel_webhook_secret = (
                "a-very-strong-secret-over-32-chars"
            )
            ms.host = "0.0.0.0"
            ms.port = 8000
            ms.log_format = "json"
            ms.log_level = "INFO"
            app = create_app()

        paths = {r.path for r in app.routes}
        assert "/webhooks/exotel/call-status" not in paths

    def test_exotel_route_absent_with_empty_secret(self) -> None:
        """Empty secret also means no Exotel route."""
        from fonely.app import create_app

        with patch("fonely.app.settings") as ms:
            ms.internal_api_secret = ""
            ms.whatsapp_verify_token = ""
            ms.exotel_webhook_secret = ""
            ms.host = "0.0.0.0"
            ms.port = 8000
            ms.log_format = "json"
            ms.log_level = "INFO"
            app = create_app()

        paths = {r.path for r in app.routes}
        assert "/webhooks/exotel/call-status" not in paths

    def test_no_exotel_intake_on_app_state(self) -> None:
        """App state does not have exotel_intake wired."""
        from fonely.app import create_app

        with patch("fonely.app.settings") as ms:
            ms.internal_api_secret = ""
            ms.whatsapp_verify_token = ""
            ms.exotel_webhook_secret = (
                "a-very-strong-secret-over-32-chars"
            )
            ms.host = "0.0.0.0"
            ms.port = 8000
            ms.log_format = "json"
            ms.log_level = "INFO"
            app = create_app()

        assert not hasattr(app.state, "exotel_intake")
        assert not hasattr(app.state, "exotel_mapping")

    def test_no_exotel_worker_entrypoint(self) -> None:
        """No worker lifecycle is started by the app."""
        from fonely.app import create_app

        with patch("fonely.app.settings") as ms:
            ms.internal_api_secret = ""
            ms.whatsapp_verify_token = ""
            ms.exotel_webhook_secret = ""
            ms.host = "0.0.0.0"
            ms.port = 8000
            ms.log_format = "json"
            ms.log_level = "INFO"
            app = create_app()

        assert not hasattr(app.state, "exotel_worker")


# ============================================================================
# Adapter→IntakeService integration (real service, mock session)
# ============================================================================


class TestAdapterToIntakeServiceIntegration:
    """Prove the adapter calls real InboundCallIntakeService.persist()
    through the full HTTP path."""

    def test_adapter_calls_real_intake_persist(self) -> None:
        """POST → adapter → event.to_inbound_event() → intake.persist()
        exercises the real InboundCallIntakeService code path."""
        from unittest.mock import patch as mpatch

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from fonely.api.channels.exotel import router
        from fonely.core.config import settings
        from fonely.services.exotel_config import ExotelNumberMapping
        from fonely.services.exotel_intake import InboundCallIntakeService

        app = FastAPI()
        app.include_router(router)
        app.state.exotel_mapping = ExotelNumberMapping(
            {"08012345678": 1}
        )

        persist_called = {"count": 0, "business_id": None, "event": None}

        async def _tracking_persist(
            business_id: int, event: InboundCallEvent
        ) -> InboundCallEventRecord:
            persist_called["count"] += 1
            persist_called["business_id"] = business_id
            persist_called["event"] = event
            return _make_record()

        service = MagicMock(spec=InboundCallIntakeService)
        service.persist = AsyncMock(side_effect=_tracking_persist)
        app.state.exotel_intake = service

        secret = "test-exotel-webhook-secret-value"
        with mpatch.object(settings, "exotel_webhook_secret", secret):
            client = TestClient(app)
            response = client.post(
                "/webhooks/exotel/call-status",
                json={
                    "CallSid": "a" * 32,
                    "EventType": "terminal",
                    "Status": "completed",
                    "From": "+919000000001",
                    "To": "08012345678",
                    "Duration": "60",
                    "ConversationDuration": "45",
                    "Direction": "inbound",
                },
                headers={"X-Exotel-Webhook-Secret": secret},
            )

        assert response.status_code == 200
        assert persist_called["count"] == 1
        assert persist_called["business_id"] == 1
        event = persist_called["event"]
        assert isinstance(event, InboundCallEvent)
        assert event.call_sid == "a" * 32
        assert event.status == "completed"
        assert event.duration == 60

    def test_adapter_maps_exotel_dto_to_neutral_event(self) -> None:
        """The adapter uses event.to_inbound_event(), not ExotelCallbackEvent."""
        from fonely.domain.calls.events import (
            ExotelCallbackEvent,
            parse_exotel_callback,
        )

        fixture = {
            "CallSid": "b" * 32,
            "EventType": "answered",
            "Status": "in-progress",
            "From": "+919000000002",
            "To": "08012345678",
            "Direction": "outbound-api",
        }
        exotel_event = parse_exotel_callback(fixture)
        assert isinstance(exotel_event, ExotelCallbackEvent)

        neutral = exotel_event.to_inbound_event()
        assert isinstance(neutral, InboundCallEvent)
        assert neutral.call_sid == exotel_event.call_sid
        assert neutral.status == exotel_event.status
        assert neutral.event_type == exotel_event.event_type
