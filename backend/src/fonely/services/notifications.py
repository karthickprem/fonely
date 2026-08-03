"""Notification outbox service — creates events inside the caller's transaction."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.enums import (
    NotificationChannel,
    NotificationEventType,
    NotificationRecipientType,
    NotificationStatus,
)
from fonely.models.schema import Business
from fonely.repositories.notifications import NotificationRepository


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = NotificationRepository(session)

    async def create_appointment_notifications(
        self,
        business_id: int,
        appointment_id: int,
        customer_phone: str,
        customer_name: str | None,
        service_name: str,
        resource_name: str,
        start_at: datetime,
        price: Any | None,
        business_timezone: str,
    ) -> list[int]:
        business = await self._session.scalar(select(Business).where(Business.id == business_id))
        clinic_name = business.name if business else "Business"
        owner_phone = business.primary_contact_phone if business else ""

        local_time = start_at.astimezone(ZoneInfo(business_timezone))
        start_local = local_time.strftime("%A, %b %d")
        time_local = local_time.strftime("%-I:%M %p")
        price_str = f"₹{price}" if price is not None else None

        event_ids: list[int] = []

        patient_event = await self._repo.insert_event_idempotent(
            {
                "business_id": business_id,
                "event_type": NotificationEventType.APPOINTMENT_CONFIRMED.value,
                "entity_type": "appointment",
                "entity_id": appointment_id,
                "recipient_type": NotificationRecipientType.PATIENT.value,
                "recipient_phone": customer_phone,
                "recipient_name": customer_name,
                "channel": NotificationChannel.WHATSAPP.value,
                "payload": {
                    "clinic_name": clinic_name,
                    "service": service_name,
                    "doctor": resource_name,
                    "date": start_local,
                    "time": time_local,
                    "price": price_str,
                    "appointment_id": appointment_id,
                },
                "status": NotificationStatus.PENDING.value,
                "idempotency_key": f"appt-confirm-patient-{appointment_id}",
            }
        )
        if patient_event is not None:
            event_ids.append(patient_event.id)

        owner_event = await self._repo.insert_event_idempotent(
            {
                "business_id": business_id,
                "event_type": NotificationEventType.APPOINTMENT_CONFIRMED.value,
                "entity_type": "appointment",
                "entity_id": appointment_id,
                "recipient_type": NotificationRecipientType.OWNER.value,
                "recipient_phone": owner_phone,
                "recipient_name": None,
                "channel": NotificationChannel.WHATSAPP.value,
                "payload": {
                    "patient_name": customer_name,
                    "patient_phone": customer_phone,
                    "service": service_name,
                    "doctor": resource_name,
                    "date": start_local,
                    "time": time_local,
                    "appointment_id": appointment_id,
                },
                "status": NotificationStatus.PENDING.value,
                "idempotency_key": f"appt-confirm-owner-{appointment_id}",
            }
        )
        if owner_event is not None:
            event_ids.append(owner_event.id)

        return event_ids

    async def create_cancellation_notifications(
        self,
        business_id: int,
        appointment_id: int,
        customer_phone: str,
        customer_name: str | None,
        service_name: str,
        resource_name: str,
        start_at: datetime,
        business_timezone: str,
        reason: str | None = None,
    ) -> list[int]:
        business = await self._session.scalar(select(Business).where(Business.id == business_id))
        clinic_name = business.name if business else "Business"
        owner_phone = business.primary_contact_phone if business else ""

        local_time = start_at.astimezone(ZoneInfo(business_timezone))
        start_local = local_time.strftime("%A, %b %d")
        time_local = local_time.strftime("%-I:%M %p")

        event_ids: list[int] = []

        patient_event = await self._repo.insert_event_idempotent(
            {
                "business_id": business_id,
                "event_type": NotificationEventType.APPOINTMENT_CANCELLED.value,
                "entity_type": "appointment",
                "entity_id": appointment_id,
                "recipient_type": NotificationRecipientType.PATIENT.value,
                "recipient_phone": customer_phone,
                "recipient_name": customer_name,
                "channel": NotificationChannel.WHATSAPP.value,
                "payload": {
                    "clinic_name": clinic_name,
                    "service": service_name,
                    "doctor": resource_name,
                    "date": start_local,
                    "time": time_local,
                    "reason": reason,
                    "appointment_id": appointment_id,
                },
                "status": NotificationStatus.PENDING.value,
                "idempotency_key": f"appt-cancel-patient-{appointment_id}",
            }
        )
        if patient_event is not None:
            event_ids.append(patient_event.id)

        owner_event = await self._repo.insert_event_idempotent(
            {
                "business_id": business_id,
                "event_type": NotificationEventType.APPOINTMENT_CANCELLED.value,
                "entity_type": "appointment",
                "entity_id": appointment_id,
                "recipient_type": NotificationRecipientType.OWNER.value,
                "recipient_phone": owner_phone,
                "recipient_name": None,
                "channel": NotificationChannel.WHATSAPP.value,
                "payload": {
                    "patient_name": customer_name,
                    "patient_phone": customer_phone,
                    "service": service_name,
                    "doctor": resource_name,
                    "date": start_local,
                    "time": time_local,
                    "reason": reason,
                    "appointment_id": appointment_id,
                },
                "status": NotificationStatus.PENDING.value,
                "idempotency_key": f"appt-cancel-owner-{appointment_id}",
            }
        )
        if owner_event is not None:
            event_ids.append(owner_event.id)

        return event_ids
