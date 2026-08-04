"""Tests for durable WhatsApp inbound event pattern."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fonely.api.channels.whatsapp import router
from fonely.repositories.inbound_events import _next_attempt_at
from fonely.workers.inbound_worker import run_inbound_worker


def _create_app(*, business_id: int | None = 1) -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    app.include_router(router)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.commit = AsyncMock()

    insert_result = MagicMock()
    insert_result.rowcount = 1
    mock_session.execute = AsyncMock(return_value=insert_result)

    mock_factory = MagicMock()
    mock_factory.return_value = mock_session
    app.state.session_factory = mock_factory

    with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as map_cls:
        mock_map = MagicMock()
        mock_map.get_business_id.return_value = business_id
        map_cls.return_value = mock_map
        app.state._mock_mapping = map_cls

    return app, mock_session


def _webhook_payload(
    message_id: str = "wamid.test1",
    text: str = "Hello",
    sender: str = "919876543210",
    phone_number_id: str = "12345",
) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": sender,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


class TestWebhookPersistsThenReturns:
    def test_returns_200_after_persisting(self) -> None:
        app, mock_session = _create_app()
        with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as map_cls:
            mock_map = MagicMock()
            mock_map.get_business_id.return_value = 1
            map_cls.return_value = mock_map
            client = TestClient(app)
            response = client.post(
                "/webhooks/whatsapp",
                json=_webhook_payload(),
            )
        assert response.status_code == 200
        mock_session.execute.assert_awaited()
        mock_session.commit.assert_awaited()

    def test_duplicate_message_id_not_inserted(self) -> None:
        app, mock_session = _create_app()
        dup_result = MagicMock()
        dup_result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=dup_result)

        with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as map_cls:
            mock_map = MagicMock()
            mock_map.get_business_id.return_value = 1
            map_cls.return_value = mock_map
            client = TestClient(app)
            response = client.post(
                "/webhooks/whatsapp",
                json=_webhook_payload(),
            )
        assert response.status_code == 200
        mock_session.commit.assert_not_awaited()

    def test_unknown_business_skipped(self) -> None:
        app, mock_session = _create_app()
        with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as map_cls:
            mock_map = MagicMock()
            mock_map.get_business_id.return_value = None
            map_cls.return_value = mock_map
            client = TestClient(app)
            response = client.post(
                "/webhooks/whatsapp",
                json=_webhook_payload(),
            )
        assert response.status_code == 200
        mock_session.execute.assert_not_awaited()

    def test_returns_503_when_session_factory_missing(self) -> None:
        app = FastAPI()
        from fonely.api.channels.whatsapp import router as wa_router

        app.include_router(wa_router)
        with patch("fonely.api.channels.whatsapp.settings") as s:
            s.whatsapp_app_secret = ""
            client = TestClient(app)
            response = client.post(
                "/webhooks/whatsapp",
                json=_webhook_payload(),
            )
        assert response.status_code == 503

    def test_returns_503_on_db_failure(self) -> None:
        app, mock_session = _create_app()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as map_cls:
            mock_map = MagicMock()
            mock_map.get_business_id.return_value = 1
            map_cls.return_value = mock_map
            client = TestClient(app)
            response = client.post(
                "/webhooks/whatsapp",
                json=_webhook_payload(),
            )
        assert response.status_code == 503

    def test_non_dict_json_returns_200(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        with patch("fonely.api.channels.whatsapp.settings") as s:
            s.whatsapp_app_secret = ""
            response = client.post(
                "/webhooks/whatsapp",
                content=b"[1, 2, 3]",
                headers={"content-type": "application/json"},
            )
        assert response.status_code == 200


class TestInboundWorker:
    @pytest.mark.asyncio
    async def test_processes_event_and_enqueues_response(self) -> None:
        mock_event = MagicMock()
        mock_event.id = 1
        mock_event.message_id = "wamid.test1"
        mock_event.business_id = 1
        mock_event.sender_phone = "919876543210"
        mock_event.message_type = "text"
        mock_event.message_body = "Hello"
        mock_event.phone_number_id = "12345"
        mock_event.attempts = 0
        mock_event.max_attempts = 5

        mock_repo = AsyncMock()
        mock_repo.claim_pending_events = AsyncMock(return_value=[mock_event])
        mock_repo.mark_domain_processed = AsyncMock()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_factory = MagicMock()
        mock_factory.return_value = mock_session

        with (
            patch(
                "fonely.workers.inbound_worker.InboundEventRepository",
                return_value=mock_repo,
            ),
            patch(
                "fonely.workers.inbound_worker._process_domain",
                new_callable=AsyncMock,
                return_value="Welcome!",
            ),
            patch(
                "fonely.workers.inbound_worker._enqueue_outbound_response",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            await run_inbound_worker(mock_factory, MagicMock(), max_iterations=1)
            mock_enqueue.assert_awaited_once()
            mock_repo.mark_domain_processed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_marks_failed_with_retry(self) -> None:
        mock_event = MagicMock()
        mock_event.id = 1
        mock_event.message_id = "wamid.fail"
        mock_event.business_id = 1
        mock_event.sender_phone = "919876543210"
        mock_event.message_type = "text"
        mock_event.message_body = "Hello"
        mock_event.phone_number_id = "12345"
        mock_event.attempts = 0
        mock_event.max_attempts = 5

        mock_repo = AsyncMock()
        mock_repo.claim_pending_events = AsyncMock(return_value=[mock_event])
        mock_repo.mark_failed = AsyncMock()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_factory = MagicMock()
        mock_factory.return_value = mock_session

        fail_repo = AsyncMock()
        fail_repo.mark_failed = AsyncMock()
        repo_call_count = 0

        def _repo_factory(session: object) -> AsyncMock:
            nonlocal repo_call_count
            repo_call_count += 1
            return mock_repo if repo_call_count == 1 else fail_repo

        with (
            patch(
                "fonely.workers.inbound_worker.InboundEventRepository",
                side_effect=_repo_factory,
            ),
            patch(
                "fonely.workers.inbound_worker._process_domain",
                new_callable=AsyncMock,
                side_effect=RuntimeError("LLM timeout"),
            ),
        ):
            await run_inbound_worker(mock_factory, MagicMock(), max_iterations=1)
            fail_repo.mark_failed.assert_awaited_once()
            call_args = fail_repo.mark_failed.call_args
            assert call_args[0][0] == 1  # business_id
            assert call_args[0][1] == 1  # event_id
            assert "RuntimeError" in call_args[0][2]

    def test_backoff_increases(self) -> None:
        t1 = _next_attempt_at(0)
        t2 = _next_attempt_at(1)
        t3 = _next_attempt_at(4)
        assert t2 > t1
        assert t3 > t2
