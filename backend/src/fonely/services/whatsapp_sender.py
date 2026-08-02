"""WhatsApp Cloud API message sender."""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("fonely.services.whatsapp_sender")

_API_BASE = "https://graph.facebook.com/v21.0"


@dataclass(frozen=True)
class WhatsAppSendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None


class WhatsAppSender:
    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._client = client

    async def send_text(self, to: str, message: str) -> WhatsAppSendResult:
        url = f"{_API_BASE}/{self._phone_number_id}/messages"
        body = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": message},
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._access_token}",
        }

        client = self._client or httpx.AsyncClient()
        owns_client = self._client is None
        try:
            response = await client.post(url, json=body, headers=headers, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            msg_id = None
            messages = data.get("messages", [])
            if messages:
                msg_id = messages[0].get("id")
            phone_suffix = to[-4:] if len(to) >= 4 else to
            logger.info(
                "whatsapp_sent",
                extra={
                    "to_suffix": phone_suffix,
                    "message_id": msg_id,
                    "success": True,
                },
            )
            return WhatsAppSendResult(success=True, message_id=msg_id)
        except httpx.TimeoutException:
            logger.warning(
                "whatsapp_send_timeout",
                extra={"to_suffix": to[-4:]},
            )
            return WhatsAppSendResult(success=False, error="timeout")
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "whatsapp_send_error",
                extra={
                    "to_suffix": to[-4:],
                    "status": exc.response.status_code,
                },
            )
            return WhatsAppSendResult(success=False, error=f"http_{exc.response.status_code}")
        except Exception:
            logger.warning("whatsapp_send_unknown_error")
            return WhatsAppSendResult(success=False, error="unknown")
        finally:
            if owns_client:
                await client.aclose()
