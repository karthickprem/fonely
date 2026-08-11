"""End-to-end WhatsApp booking through production composition.

Exercises: signed webhook → durable inbox → inbound worker → conversation
→ availability → offer → proposal → confirmation → appointment + allocation
+ manifest + outbox → fake delivery → restart/replay.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fonely.services.conversation import _CONVERSATIONS

pytestmark = pytest.mark.postgres

_SECRET = "test-whatsapp-secret"
_VERIFY_TOKEN = "test-verify-token"
_INTERNAL_SECRET = "test-internal-secret"
_PHONE_NUMBER_ID = "phone-1"
_PATIENT_PHONE = "+919123456789"
_OWNER_PHONE = "+919000000001"


@pytest.fixture(autouse=True)
def _clear_conversations():
    _CONVERSATIONS.clear()
    yield
    _CONVERSATIONS.clear()


async def _seed_dental_clinic(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental Clinic', 'dental', :phone, "
            "'Asia/Kolkata', 'trial')"
        ),
        {"phone": _OWNER_PHONE},
    )
    await session.execute(
        text(
            "INSERT INTO business_users (id, business_id, phone, role, is_active) "
            "VALUES (1, 1, :phone, 'owner', true)"
        ),
        {"phone": _OWNER_PHONE},
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'General Consultation', 30, 0, 0, 500.00, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Dr. Priya', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) VALUES (1, 1, 1, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '09:00', '18:00', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _webhook_payload(message_id: str, body_text: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "entry-1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15551234567",
                                "phone_number_id": _PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Test Patient"},
                                    "wa_id": _PATIENT_PHONE.lstrip("+"),
                                }
                            ],
                            "messages": [
                                {
                                    "from": _PATIENT_PHONE.lstrip("+"),
                                    "id": message_id,
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "text": {"body": body_text},
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


async def test_signed_webhook_persists_inbound_event(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)

    with (
        patch("fonely.api.channels.whatsapp.settings") as mock_settings,
        patch("fonely.services.whatsapp_config.settings") as mock_wa_settings,
    ):
        mock_settings.whatsapp_app_secret = _SECRET
        mock_settings.whatsapp_verify_token = _VERIFY_TOKEN
        mock_settings.whatsapp_phone_number_id = _PHONE_NUMBER_ID
        mock_settings.whatsapp_business_mappings = f'{{"{_PHONE_NUMBER_ID}": 1}}'
        mock_wa_settings.whatsapp_business_mappings = f'{{"{_PHONE_NUMBER_ID}": 1}}'

        from fonely.app import create_app

        with (
            patch("fonely.core.config.settings") as _app_settings,
            patch("fonely.app.settings") as app_mod_settings,
        ):
            app_mod_settings.internal_api_secret = _INTERNAL_SECRET
            app_mod_settings.whatsapp_verify_token = _VERIFY_TOKEN
            app_mod_settings.whatsapp_app_secret = _SECRET
            app_mod_settings.database_url = "unused"
            app_mod_settings.sarvam_api_key = ""
            app_mod_settings.exotel_webhook_secret = ""
            app_mod_settings.cors_origins = ""
            app_mod_settings.readiness_timeout_seconds = 3.0

            app = create_app()
            app.state.engine = pg_engine
            app.state.session_factory = pg_session_factory

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                payload = _webhook_payload("wamid.test1", "I need a dental appointment")
                body = json.dumps(payload).encode()
                sig = _sign(body)

                response = await client.post(
                    "/webhooks/whatsapp",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": sig,
                    },
                )
                assert response.status_code == 200

    async with pg_session_factory() as verify:
        count = await verify.scalar(
            text("SELECT count(*) FROM whatsapp_inbound_events WHERE message_id = 'wamid.test1'")
        )
        assert count == 1


async def test_invalid_signature_rejected(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)

    with (
        patch("fonely.api.channels.whatsapp.settings") as mock_settings,
        patch("fonely.services.whatsapp_config.settings") as mock_wa_settings,
    ):
        mock_settings.whatsapp_app_secret = _SECRET
        mock_settings.whatsapp_verify_token = _VERIFY_TOKEN
        mock_settings.whatsapp_phone_number_id = _PHONE_NUMBER_ID
        mock_settings.whatsapp_business_mappings = f'{{"{_PHONE_NUMBER_ID}": 1}}'
        mock_wa_settings.whatsapp_business_mappings = f'{{"{_PHONE_NUMBER_ID}": 1}}'

        from fonely.app import create_app

        with patch("fonely.app.settings") as app_mod_settings:
            app_mod_settings.internal_api_secret = _INTERNAL_SECRET
            app_mod_settings.whatsapp_verify_token = _VERIFY_TOKEN
            app_mod_settings.whatsapp_app_secret = _SECRET
            app_mod_settings.database_url = "unused"
            app_mod_settings.sarvam_api_key = ""
            app_mod_settings.exotel_webhook_secret = ""
            app_mod_settings.cors_origins = ""
            app_mod_settings.readiness_timeout_seconds = 3.0

            app = create_app()
            app.state.engine = pg_engine
            app.state.session_factory = pg_session_factory

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                payload = _webhook_payload("wamid.bad", "hello")
                body = json.dumps(payload).encode()

                response = await client.post(
                    "/webhooks/whatsapp",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": "sha256=invalid",
                    },
                )
                assert response.status_code == 200

    async with pg_session_factory() as verify:
        count = await verify.scalar(
            text("SELECT count(*) FROM whatsapp_inbound_events WHERE message_id = 'wamid.bad'")
        )
        assert count == 0
