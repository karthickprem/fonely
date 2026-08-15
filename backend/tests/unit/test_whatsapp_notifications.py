"""Tests for WhatsApp notification sender and message formatting."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from fonely.services.whatsapp_notification_sender import (
    NotificationDeliveryError,
    WhatsAppNotificationSender,
)
from fonely.services.whatsapp_sender import WhatsAppSendResult


def _mock_event(
    *,
    event_type: str = "appointment_confirmed",
    recipient_type: str = "patient",
    recipient_phone: str = "+919123456789",
    payload: dict | None = None,
) -> MagicMock:
    event = MagicMock()
    event.id = 1
    event.event_type = event_type
    event.recipient_type = recipient_type
    event.recipient_phone = recipient_phone
    event.payload = payload or {
        "clinic_name": "Smile Dental",
        "service": "General Consultation",
        "doctor": "Dr. Priya",
        "date": "Tuesday, Aug 12",
        "time": "6:30 PM",
        "price": "₹300",
        "appointment_id": 42,
    }
    return event


class TestPatientConfirmationFormat:
    def test_contains_all_fields(self) -> None:
        sender = WhatsAppNotificationSender(MagicMock())
        event = _mock_event()
        msg = sender._format_message(event)
        assert "✓ Appointment confirmed" in msg
        assert "Smile Dental" in msg
        assert "General Consultation" in msg
        assert "Dr. Priya" in msg
        assert "Tuesday, Aug 12" in msg
        assert "6:30 PM" in msg
        assert "₹300" in msg
        assert "cancel or reschedule" in msg

    def test_is_short_and_scannable(self) -> None:
        sender = WhatsAppNotificationSender(MagicMock())
        event = _mock_event()
        msg = sender._format_message(event)
        lines = msg.strip().split("\n")
        assert len(lines) <= 12


class TestPatientCancellationFormat:
    def test_includes_reason(self) -> None:
        sender = WhatsAppNotificationSender(MagicMock())
        event = _mock_event(
            event_type="appointment_cancelled",
            payload={
                "clinic_name": "Smile Dental",
                "service": "Consultation",
                "doctor": "Dr. Priya",
                "date": "Tuesday, Aug 12",
                "time": "6:30 PM",
                "reason": "Doctor on leave",
            },
        )
        msg = sender._format_message(event)
        assert "cancelled" in msg
        assert "Doctor on leave" in msg
        assert "rebook" in msg


class TestOwnerNotificationFormat:
    def test_includes_patient_phone(self) -> None:
        sender = WhatsAppNotificationSender(MagicMock())
        event = _mock_event(
            recipient_type="owner",
            payload={
                "patient_name": "Karthick",
                "patient_phone": "+919123456789",
                "service": "Consultation",
                "doctor": "Dr. Priya",
                "date": "Tuesday, Aug 12",
                "time": "6:30 PM",
                "price": "₹300",
            },
        )
        msg = sender._format_message(event)
        assert "New appointment booked" in msg
        assert "Karthick" in msg
        assert "+919123456789" in msg

    def test_owner_cancellation(self) -> None:
        sender = WhatsAppNotificationSender(MagicMock())
        event = _mock_event(
            event_type="appointment_cancelled",
            recipient_type="owner",
            payload={
                "patient_name": "Karthick",
                "service": "Consultation",
                "doctor": "Dr. Priya",
                "date": "Tuesday, Aug 12",
                "time": "6:30 PM",
                "reason": "Patient requested",
            },
        )
        msg = sender._format_message(event)
        assert "Appointment cancelled" in msg
        assert "Patient requested" in msg


class TestCallbackNotificationFormat:
    def test_full_facts_render(self) -> None:
        sender = WhatsAppNotificationSender(MagicMock())
        event = _mock_event(
            event_type="callback_requested",
            recipient_type="owner",
            payload={
                "caller_phone": "+919123456789",
                "service_name": "General Consultation",
                "target_date": "2026-08-20",
                "attempted_candidates": ["Dr. Priya Kumar", "Dr. Priya Rao"],
                "reason_code": "doctor_disambiguation_exhausted",
            },
        )
        msg = sender._format_message(event)
        # The owner can COMPLETE the callback from this: the number, the service,
        # the date, and what the caller was stuck between.
        assert "+919123456789" in msg
        assert "General Consultation" in msg
        assert "2026-08-20" in msg
        assert "Dr. Priya Kumar" in msg and "Dr. Priya Rao" in msg
        assert "call them back" in msg.lower()

    def test_degrades_gracefully_when_facts_missing(self) -> None:
        # The give-up happens when facts couldn't be resolved, so service/date/
        # candidates may be absent. The message must still name the number and
        # never print blank "on " / "wanted: " lines.
        sender = WhatsAppNotificationSender(MagicMock())
        event = _mock_event(
            event_type="callback_requested",
            recipient_type="owner",
            payload={
                "caller_phone": "+919123456789",
                "service_name": None,
                "target_date": None,
                "attempted_candidates": [],
                "reason_code": "slot_disambiguation_exhausted",
            },
        )
        msg = sender._format_message(event)
        assert "+919123456789" in msg
        assert "They wanted" not in msg, "no blank facts line when service/date absent"
        assert "couldn't pick between" not in msg, "no empty candidates line"

    def test_missing_caller_phone_falls_back_not_blank(self) -> None:
        sender = WhatsAppNotificationSender(MagicMock())
        event = _mock_event(
            event_type="callback_requested",
            recipient_type="owner",
            payload={"caller_phone": "", "attempted_candidates": []},
        )
        msg = sender._format_message(event)
        assert "follow up with the caller" in msg.lower()


class TestUnknownEventType:
    def test_fallback_message(self) -> None:
        sender = WhatsAppNotificationSender(MagicMock())
        event = _mock_event(event_type="some_unknown_type")
        msg = sender._format_message(event)
        assert "Notification from" in msg


class TestDelivery:
    @pytest.mark.asyncio
    async def test_successful_delivery(self) -> None:
        wa_sender = AsyncMock()
        wa_sender.send_text = AsyncMock(
            return_value=WhatsAppSendResult(success=True, message_id="msg-1")
        )
        sender = WhatsAppNotificationSender(wa_sender)
        event = _mock_event()
        await sender.send(event)
        wa_sender.send_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delivery_failure_raises(self) -> None:
        wa_sender = AsyncMock()
        wa_sender.send_text = AsyncMock(
            return_value=WhatsAppSendResult(success=False, error="timeout")
        )
        sender = WhatsAppNotificationSender(wa_sender)
        event = _mock_event()
        with pytest.raises(NotificationDeliveryError):
            await sender.send(event)


class TestWorkerSenderSelection:
    def test_uses_whatsapp_when_configured(self) -> None:
        from unittest.mock import patch

        import run_worker

        from fonely.core.config import Settings

        s = Settings(whatsapp_access_token="test-token")
        with patch.object(run_worker, "settings", s):
            sender = run_worker._create_sender(MagicMock())
        assert type(sender).__name__ == "WhatsAppNotificationSender"
        # Channel identity must come from the database, not from process config.
        assert type(sender._resolver).__name__ == "DatabaseWhatsAppSenderResolver"

    def test_fails_closed_when_not_configured(self) -> None:
        from unittest.mock import patch

        import run_worker

        from fonely.core.config import Settings

        s = Settings(whatsapp_access_token="")
        with (
            patch.object(run_worker, "settings", s),
            pytest.raises(RuntimeError, match="WHATSAPP_ACCESS_TOKEN"),
        ):
            run_worker._create_sender(MagicMock())
