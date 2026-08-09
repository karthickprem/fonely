"""Notification outbox service — creates events inside the caller's transaction."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.config import settings
from fonely.models.enums import (
    NotificationChannel,
    NotificationEventType,
    NotificationRecipientType,
    NotificationStatus,
)
from fonely.models.schema import Business
from fonely.repositories.notifications import NotificationRepository
from fonely.services.whatsapp_config import WhatsAppBusinessMapping


class NotificationIdempotencyConflictError(RuntimeError):
    pass


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = NotificationRepository(session)

    async def _insert_or_verify(self, values: dict[str, Any]) -> int | None:
        event = await self._repo.insert_event_idempotent(values)
        if event is not None:
            return event.id
        existing = await self._repo.get_event_by_idempotency_key(values["idempotency_key"])
        if existing is None:
            return None
        if (
            existing.business_id != values["business_id"]
            or existing.event_type != values["event_type"]
            or existing.recipient_type != values["recipient_type"]
            or existing.entity_type != values["entity_type"]
            or existing.entity_id != values["entity_id"]
        ):
            raise NotificationIdempotencyConflictError(
                f"Notification key {values['idempotency_key']} exists "
                f"with conflicting tenant/event/recipient/entity"
            )
        return None

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
        phone_number_id = WhatsAppBusinessMapping().get_phone_number_id(
            business_id, preferred=settings.whatsapp_phone_number_id or None
        )
        if phone_number_id is None:
            raise RuntimeError("whatsapp_business_mapping_missing_or_ambiguous")

        local_time = start_at.astimezone(ZoneInfo(business_timezone))
        start_local = local_time.strftime("%A, %b %d")
        time_local = local_time.strftime("%-I:%M %p")
        price_str = f"₹{price}" if price is not None else None

        event_ids: list[int] = []

        patient_id = await self._insert_or_verify(
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
                    "phone_number_id": phone_number_id,
                },
                "status": NotificationStatus.PENDING.value,
                "idempotency_key": f"appt-confirm-patient-{appointment_id}",
            }
        )
        if patient_id is not None:
            event_ids.append(patient_id)

        owner_id = await self._insert_or_verify(
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
                    "phone_number_id": phone_number_id,
                },
                "status": NotificationStatus.PENDING.value,
                "idempotency_key": f"appt-confirm-owner-{appointment_id}",
            }
        )
        if owner_id is not None:
            event_ids.append(owner_id)

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
        phone_number_id = WhatsAppBusinessMapping().get_phone_number_id(
            business_id, preferred=settings.whatsapp_phone_number_id or None
        )
        if phone_number_id is None:
            raise RuntimeError("whatsapp_business_mapping_missing_or_ambiguous")

        local_time = start_at.astimezone(ZoneInfo(business_timezone))
        start_local = local_time.strftime("%A, %b %d")
        time_local = local_time.strftime("%-I:%M %p")

        event_ids: list[int] = []

        patient_id = await self._insert_or_verify(
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
                    "phone_number_id": phone_number_id,
                },
                "status": NotificationStatus.PENDING.value,
                "idempotency_key": f"appt-cancel-patient-{appointment_id}",
            }
        )
        if patient_id is not None:
            event_ids.append(patient_id)

        owner_id = await self._insert_or_verify(
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
                    "phone_number_id": phone_number_id,
                },
                "status": NotificationStatus.PENDING.value,
                "idempotency_key": f"appt-cancel-owner-{appointment_id}",
            }
        )
        if owner_id is not None:
            event_ids.append(owner_id)

        return event_ids

    async def create_reschedule_notifications(
        self,
        business_id: int,
        appointment_id: int,
        pending_action_id: int,
        customer_phone: str,
        customer_name: str | None,
        service_name: str,
        resource_name: str,
        old_start_at: datetime,
        new_start_at: datetime,
        business_timezone: str,
    ) -> list[int]:
        business = await self._session.scalar(select(Business).where(Business.id == business_id))
        clinic_name = business.name if business else "Business"
        owner_phone = business.primary_contact_phone if business else ""
        phone_number_id = WhatsAppBusinessMapping().get_phone_number_id(
            business_id, preferred=settings.whatsapp_phone_number_id or None
        )
        if phone_number_id is None:
            raise RuntimeError("whatsapp_business_mapping_missing_or_ambiguous")

        tz = ZoneInfo(business_timezone)
        old_local = old_start_at.astimezone(tz)
        new_local = new_start_at.astimezone(tz)
        old_date_str = old_local.strftime("%A, %b %d")
        old_time_str = old_local.strftime("%-I:%M %p")
        new_date_str = new_local.strftime("%A, %b %d")
        new_time_str = new_local.strftime("%-I:%M %p")

        event_ids: list[int] = []

        patient_id = await self._insert_or_verify(
            {
                "business_id": business_id,
                "event_type": NotificationEventType.APPOINTMENT_RESCHEDULED.value,
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
                    "old_date": old_date_str,
                    "old_time": old_time_str,
                    "new_date": new_date_str,
                    "new_time": new_time_str,
                    "appointment_id": appointment_id,
                    "phone_number_id": phone_number_id,
                },
                "status": NotificationStatus.PENDING.value,
                "idempotency_key": f"appt-resched-patient-{appointment_id}-{pending_action_id}",
            }
        )
        if patient_id is not None:
            event_ids.append(patient_id)

        owner_id = await self._insert_or_verify(
            {
                "business_id": business_id,
                "event_type": NotificationEventType.APPOINTMENT_RESCHEDULED.value,
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
                    "old_date": old_date_str,
                    "old_time": old_time_str,
                    "new_date": new_date_str,
                    "new_time": new_time_str,
                    "appointment_id": appointment_id,
                    "phone_number_id": phone_number_id,
                },
                "status": NotificationStatus.PENDING.value,
                "idempotency_key": f"appt-resched-owner-{appointment_id}-{pending_action_id}",
            }
        )
        if owner_id is not None:
            event_ids.append(owner_id)

        return event_ids
