"""Tests for durable WhatsApp inbound event pattern."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fonely.api.channels.whatsapp import router
from fonely.repositories.inbound_events import _next_attempt_at, deterministic_lock_key
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
            response = client.post("/webhooks/whatsapp", json=_webhook_payload())
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
            response = client.post("/webhooks/whatsapp", json=_webhook_payload())
        assert response.status_code == 200
        mock_session.commit.assert_not_awaited()

    def test_unknown_business_skipped(self) -> None:
        app, mock_session = _create_app()
        with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as map_cls:
            mock_map = MagicMock()
            mock_map.get_business_id.return_value = None
            map_cls.return_value = mock_map
            client = TestClient(app)
            response = client.post("/webhooks/whatsapp", json=_webhook_payload())
        assert response.status_code == 200
        mock_session.execute.assert_not_awaited()

    def test_returns_503_when_session_factory_missing(self) -> None:
        app = FastAPI()
        from fonely.api.channels.whatsapp import router as wa_router

        app.include_router(wa_router)
        with patch("fonely.api.channels.whatsapp.settings") as s:
            s.whatsapp_app_secret = ""
            client = TestClient(app)
            response = client.post("/webhooks/whatsapp", json=_webhook_payload())
        assert response.status_code == 503

    def test_returns_503_on_db_failure(self) -> None:
        app, mock_session = _create_app()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as map_cls:
            mock_map = MagicMock()
            mock_map.get_business_id.return_value = 1
            map_cls.return_value = mock_map
            client = TestClient(app)
            response = client.post("/webhooks/whatsapp", json=_webhook_payload())
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


class TestInboundWorkerPhases:
    @pytest.mark.asyncio
    async def test_three_phase_success(self) -> None:
        with (
            patch(
                "fonely.workers.inbound_worker._phase_a_claim",
                new_callable=AsyncMock,
                return_value=MagicMock(
                    event_id=1,
                    business_id=1,
                    message_id="wamid.1",
                    sender_phone="919876543210",
                    message_type="text",
                    message_body="Hello",
                    phone_number_id="12345",
                    claim_token="tok",
                    attempts=0,
                    max_attempts=5,
                ),
            ),
            patch(
                "fonely.workers.inbound_worker._phase_b_reason",
                new_callable=AsyncMock,
                return_value="Welcome!",
            ) as mock_b,
            patch(
                "fonely.workers.inbound_worker._phase_c_commit",
                new_callable=AsyncMock,
            ) as mock_c,
        ):
            await run_inbound_worker(MagicMock(), MagicMock(), max_iterations=1)
            mock_b.assert_awaited_once()
            mock_c.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_phase_b_failure_marks_failed(self) -> None:
        claimed = MagicMock(
            event_id=1,
            business_id=1,
            message_id="wamid.fail",
            sender_phone="919876543210",
            attempts=0,
            max_attempts=5,
        )
        with (
            patch(
                "fonely.workers.inbound_worker._phase_a_claim",
                new_callable=AsyncMock,
                return_value=claimed,
            ),
            patch(
                "fonely.workers.inbound_worker._phase_b_reason",
                new_callable=AsyncMock,
                side_effect=RuntimeError("LLM timeout"),
            ),
            patch(
                "fonely.workers.inbound_worker._handle_failure",
                new_callable=AsyncMock,
            ) as mock_fail,
        ):
            await run_inbound_worker(MagicMock(), MagicMock(), max_iterations=1)
            mock_fail.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_event_does_not_process(self) -> None:
        with patch(
            "fonely.workers.inbound_worker._phase_a_claim",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await run_inbound_worker(MagicMock(), MagicMock(), max_iterations=1)


class TestDeterministicLockKey:
    def test_same_input_same_key(self) -> None:
        k1 = deterministic_lock_key(1, "+919876543210")
        k2 = deterministic_lock_key(1, "+919876543210")
        assert k1 == k2

    def test_different_input_different_key(self) -> None:
        k1 = deterministic_lock_key(1, "+919876543210")
        k2 = deterministic_lock_key(2, "+919876543210")
        assert k1 != k2

    def test_key_is_signed_int(self) -> None:
        key = deterministic_lock_key(1, "+919876543210")
        assert isinstance(key, int)
        assert -(2**63) <= key <= 2**63 - 1


class TestBackoff:
    def test_backoff_increases(self) -> None:
        t1 = _next_attempt_at(0)
        t2 = _next_attempt_at(1)
        t3 = _next_attempt_at(4)
        assert t2 > t1
        assert t3 > t2

    def test_first_backoff_is_30s(self) -> None:
        import time

        before = time.time()
        t = _next_attempt_at(0)
        delta = t.timestamp() - before
        assert 25 < delta < 35
