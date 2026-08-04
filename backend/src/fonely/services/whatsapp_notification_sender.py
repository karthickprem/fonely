"""WhatsApp notification delivery with trusted channel identity resolution."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from fonely.models.schema import NotificationOutboxEvent
from fonely.services.whatsapp_sender import WhatsAppSender

logger = logging.getLogger("fonely.services.whatsapp_notification")


class NotificationDeliveryError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error
        self.ambiguous = error in {"timeout", "unknown"}


@dataclass(frozen=True)
class DeliveryReceipt:
    provider_message_id: str | None
    final: bool = False


class WhatsAppSenderResolver(Protocol):
    def resolve(self, business_id: int, phone_number_id: str) -> WhatsAppSender: ...


class ConfiguredWhatsAppSenderResolver:
    """Resolve trusted tenant/phone identity using configured business mapping."""

    def __init__(
        self,
        *,
        access_token: str,
        business_mappings: dict[str, int],
        client: object | None = None,
    ) -> None:
        self._access_token = access_token
        self._business_mappings = business_mappings
        self._client = client
        self._senders: dict[str, WhatsAppSender] = {}

    def resolve(self, business_id: int, phone_number_id: str) -> WhatsAppSender:
        mapped_business = self._business_mappings.get(phone_number_id)
        if mapped_business != business_id:
            raise NotificationDeliveryError("channel_identity_mismatch")
        sender = self._senders.get(phone_number_id)
        if sender is None:
            sender = WhatsAppSender(
                access_token=self._access_token,
                phone_number_id=phone_number_id,
                client=self._client,  # type: ignore[arg-type]
            )
            self._senders[phone_number_id] = sender
        return sender


class WhatsAppNotificationSender:
    def __init__(
        self,
        sender: WhatsAppSender | None = None,
        *,
        resolver: WhatsAppSenderResolver | None = None,
    ) -> None:
        if sender is None and resolver is None:
            raise ValueError("sender or resolver is required")
        self._sender = sender
        self._resolver = resolver

    async def send(self, event: NotificationOutboxEvent) -> DeliveryReceipt:
        message = self._format_message(event)
        sender = self._resolve_sender(event)
        result = await sender.send_text(event.recipient_phone, message)
        if not result.success:
            raise NotificationDeliveryError(result.error or "unknown")
        return DeliveryReceipt(provider_message_id=result.message_id)

    def _resolve_sender(self, event: NotificationOutboxEvent) -> WhatsAppSender:
        payload = event.payload or {}
        phone_number_id = payload.get("phone_number_id")
        if self._resolver is not None:
            if not isinstance(phone_number_id, str) or not phone_number_id:
                raise NotificationDeliveryError("missing_phone_number_id")
            return self._resolver.resolve(event.business_id, phone_number_id)
        assert self._sender is not None
        return self._sender

    def _format_message(self, event: NotificationOutboxEvent) -> str:
        payload = event.payload or {}
        event_type = event.event_type
        recipient_type = event.recipient_type

        if event_type == "appointment_confirmed":
            if recipient_type == "patient":
                return self._format_patient_confirmation(payload)
            if recipient_type == "owner":
                return self._format_owner_booking(payload)

        if event_type == "appointment_cancelled":
            if recipient_type == "patient":
                return self._format_patient_cancellation(payload)
            if recipient_type == "owner":
                return self._format_owner_cancellation(payload)

        if event_type == "whatsapp_inbound_response":
            return str(payload.get("response_text", ""))

        clinic = payload.get("clinic_name", "your clinic")
        return f"Notification from {clinic}."

    @staticmethod
    def _format_patient_confirmation(p: dict[str, Any]) -> str:
        lines = [
            "✓ Appointment confirmed",
            p.get("clinic_name", ""),
            f"Service: {p.get('service', '')}",
            f"Doctor: {p.get('doctor', '')}",
            f"Date: {p.get('date', '')}",
            f"Time: {p.get('time', '')}",
        ]
        if p.get("price"):
            lines.append(f"Fee: {p['price']}")
        lines.append("")
        lines.append("To cancel or reschedule, reply to this message.")
        return "\n".join(lines)

    @staticmethod
    def _format_patient_cancellation(p: dict[str, Any]) -> str:
        lines = [
            "Your appointment has been cancelled.",
            p.get("clinic_name", ""),
            f"Service: {p.get('service', '')}",
            f"Doctor: {p.get('doctor', '')}",
            f"Date: {p.get('date', '')}",
            f"Time: {p.get('time', '')}",
        ]
        if p.get("reason"):
            lines.append(f"\nReason: {p['reason']}")
        lines.append("")
        lines.append("To rebook, reply to this message.")
        return "\n".join(lines)

    @staticmethod
    def _format_owner_booking(p: dict[str, Any]) -> str:
        patient = p.get("patient_name") or "Patient"
        phone = p.get("patient_phone", "")
        lines = [
            "New appointment booked",
            f"Patient: {patient} ({phone})",
            f"Service: {p.get('service', '')}",
            f"Doctor: {p.get('doctor', '')}",
            f"Date: {p.get('date', '')}",
            f"Time: {p.get('time', '')}",
        ]
        if p.get("price"):
            lines.append(f"Fee: {p['price']}")
        return "\n".join(lines)

    @staticmethod
    def _format_owner_cancellation(p: dict[str, Any]) -> str:
        patient = p.get("patient_name") or "Patient"
        lines = [
            "Appointment cancelled",
            f"Patient: {patient}",
            f"Service: {p.get('service', '')}",
            f"Doctor: {p.get('doctor', '')}",
            f"Date: {p.get('date', '')}",
            f"Time: {p.get('time', '')}",
        ]
        if p.get("reason"):
            lines.append(f"Reason: {p['reason']}")
        return "\n".join(lines)
