"""WhatsApp notification sender — delivers outbox events via WhatsApp Cloud API."""

from __future__ import annotations

import logging
from typing import Any

from fonely.models.schema import NotificationOutboxEvent
from fonely.services.whatsapp_sender import WhatsAppSender

logger = logging.getLogger("fonely.services.whatsapp_notification")


class NotificationDeliveryError(Exception):
    pass


class WhatsAppNotificationSender:
    def __init__(self, sender: WhatsAppSender) -> None:
        self._sender = sender

    async def send(self, event: NotificationOutboxEvent) -> None:
        message = self._format_message(event)
        result = await self._sender.send_text(event.recipient_phone, message)
        if not result.success:
            raise NotificationDeliveryError(result.error or "unknown")

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
