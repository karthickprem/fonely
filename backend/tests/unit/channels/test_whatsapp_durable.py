"""Tests for durable WhatsApp inbound event pattern."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

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

    app.state._resolved_business_id = business_id
    return app, mock_session


@contextmanager
def _patch_channels(business_id: int | None):
    """Stand in for the business_whatsapp_channels lookup.

    Inbound tenant resolution reads the database as of migration 0016. These
    tests are about webhook durability, not routing, so the repository is
    replaced wholesale — which also keeps session.execute assertions below
    measuring only the event insert.
    """
    with patch("fonely.api.channels.whatsapp.WhatsAppChannelRepository") as repo_cls:
        repo = MagicMock()
        repo.resolve_business_id = AsyncMock(return_value=business_id)
        repo_cls.return_value = repo
        yield repo_cls


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


def _signed_post(client: TestClient, payload: dict) -> object:
    import hashlib
    import hmac as _hmac

    body = json.dumps(payload).encode()
    sig = "sha256=" + _hmac.new(b"test-app-secret", body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"content-type": "application/json", "X-Hub-Signature-256": sig},
    )


class TestWebhookPersistsThenReturns:
    def test_returns_200_after_persisting(self) -> None:
        app, mock_session = _create_app()
        with (
            _patch_channels(1),
            patch("fonely.api.channels.whatsapp.settings") as s,
        ):
            s.whatsapp_app_secret = "test-app-secret"
            client = TestClient(app)
            response = _signed_post(client, _webhook_payload())
        assert response.status_code == 200
        mock_session.execute.assert_awaited()
        mock_session.commit.assert_awaited()

    def test_duplicate_message_id_not_inserted(self) -> None:
        app, mock_session = _create_app()
        dup_result = MagicMock()
        dup_result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=dup_result)
        with (
            _patch_channels(1),
            patch("fonely.api.channels.whatsapp.settings") as s,
        ):
            s.whatsapp_app_secret = "test-app-secret"
            client = TestClient(app)
            response = _signed_post(client, _webhook_payload())
        assert response.status_code == 200
        mock_session.commit.assert_not_awaited()

    def test_unknown_business_skipped(self) -> None:
        app, mock_session = _create_app()
        with (
            _patch_channels(None),
            patch("fonely.api.channels.whatsapp.settings") as s,
        ):
            s.whatsapp_app_secret = "test-app-secret"
            client = TestClient(app)
            response = _signed_post(client, _webhook_payload())
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
        with (
            _patch_channels(1),
            patch("fonely.api.channels.whatsapp.settings") as s,
        ):
            s.whatsapp_app_secret = "test-app-secret"
            client = TestClient(app)
            response = _signed_post(client, _webhook_payload())
        assert response.status_code == 503

    def test_missing_secret_returns_503(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        with patch("fonely.api.channels.whatsapp.settings") as s:
            s.whatsapp_app_secret = ""
            response = client.post("/webhooks/whatsapp", json=_webhook_payload())
        assert response.status_code == 503


class TestInboundWorkerLoop:
    @pytest.mark.asyncio
    async def test_no_event_does_not_call_provider(self) -> None:
        provider = AsyncMock()
        with patch(
            "fonely.workers.inbound_worker._claim",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await run_inbound_worker(MagicMock(), provider, max_iterations=1)
        provider.complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claim_is_processed_once(self) -> None:
        claimed = MagicMock()
        with (
            patch(
                "fonely.workers.inbound_worker._claim",
                new_callable=AsyncMock,
                return_value=claimed,
            ),
            patch(
                "fonely.workers.inbound_worker._process_claimed",
                new_callable=AsyncMock,
            ) as process,
        ):
            await run_inbound_worker(MagicMock(), MagicMock(), max_iterations=1)
        process.assert_awaited_once_with(ANY, claimed, ANY)


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

    def test_key_is_stable_across_python_hash_seeds(self) -> None:
        code = (
            "from fonely.repositories.inbound_events import deterministic_lock_key; "
            "print(deterministic_lock_key(1, '+919876543210'))"
        )
        values = []
        for seed in ("1", "999"):
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = seed
            env["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
            result = subprocess.run(
                [sys.executable, "-c", code],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            values.append(result.stdout.strip())
        assert values[0] == values[1]


class TestDeploymentConfiguration:
    def test_dockerfile_copies_worker_entrypoints(self) -> None:
        dockerfile = (Path(__file__).parents[3] / "Dockerfile").read_text()
        assert "run_worker.py" in dockerfile
        assert "run_inbound_worker.py" in dockerfile

    def test_compose_has_worker_restart_health_and_required_env(self) -> None:
        compose = (Path(__file__).parents[4] / "docker-compose.staging.yml").read_text()
        assert "inbound-worker:" in compose
        assert "notification-worker:" in compose
        assert compose.count("restart: unless-stopped") >= 2
        assert compose.count("disable: true") >= 2
        assert "SARVAM_API_KEY: ${SARVAM_API_KEY:?" in compose
        assert "WHATSAPP_ACCESS_TOKEN: ${WHATSAPP_ACCESS_TOKEN:?" in compose
        # Channel identity is database state (migration 0016), not deploy
        # config. A required env var here would block staging startup over a
        # value nothing reads, and would invite re-introducing the per-process
        # mapping that made onboarding a clinic a deploy.
        assert "WHATSAPP_BUSINESS_MAPPINGS" not in compose
        assert "WHATSAPP_PHONE_NUMBER_ID" not in compose


class TestBackoff:
    def test_backoff_increases(self) -> None:
        t1 = _next_attempt_at(1)
        t2 = _next_attempt_at(2)
        t3 = _next_attempt_at(5)
        assert t2 > t1
        assert t3 > t2

    def test_first_backoff_is_30s(self) -> None:
        import time

        before = time.time()
        t = _next_attempt_at(1)
        delta = t.timestamp() - before
        assert 25 < delta < 35
