"""Notification outbox service — creates events inside the caller's transaction."""

import hashlib
import json
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
from fonely.models.schema import Business, BusinessUser
from fonely.repositories.notifications import NotificationRepository
from fonely.services.whatsapp_config import WhatsAppBusinessMapping


class NotificationIdempotencyConflictError(RuntimeError):
    pass


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = NotificationRepository(session)

    @staticmethod
    def _payload_digest(payload: object) -> str:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def _insert_or_verify(self, values: dict[str, Any]) -> int | None:
        event = await self._repo.insert_event_idempotent(values)
        if event is not None:
            return event.id
        business_id = int(values["business_id"])
        idempotency_key = str(values["idempotency_key"])
        existing = await self._repo.get_event_by_idempotency_key(business_id, idempotency_key)
        if existing is None:
            global_existing = await self._repo.get_event_by_global_idempotency_key(idempotency_key)
            if global_existing is not None:
                raise NotificationIdempotencyConflictError(
                    "Notification idempotency key belongs to another business"
                )
            raise NotificationIdempotencyConflictError(
                "Notification idempotency conflict has no equivalent durable event"
            )
        if (
            existing.business_id != business_id
            or existing.event_type != values["event_type"]
            or existing.recipient_type != values["recipient_type"]
            or existing.recipient_phone != values["recipient_phone"]
            or existing.recipient_name != values.get("recipient_name")
            or existing.channel != values["channel"]
            or existing.entity_type != values["entity_type"]
            or existing.entity_id != values["entity_id"]
            or self._payload_digest(existing.payload) != self._payload_digest(values["payload"])
            or existing.status
            not in {
                NotificationStatus.PENDING.value,
                NotificationStatus.PROCESSING.value,
                NotificationStatus.DELIVERED.value,
                NotificationStatus.FAILED.value,
                NotificationStatus.DEAD_LETTER.value,
                NotificationStatus.UNKNOWN.value,
            }
        ):
            raise NotificationIdempotencyConflictError(
                "Notification idempotency key exists with non-equivalent durable evidence"
            )
        return existing.id

    async def _business_and_owner_phone(self, business_id: int) -> tuple[Business, str]:
        business = await self._session.scalar(select(Business).where(Business.id == business_id))
        if business is None:
            raise RuntimeError("notification_business_not_found")
        owners = list(
            (
                await self._session.scalars(
                    select(BusinessUser).where(
                        BusinessUser.business_id == business_id,
                        BusinessUser.role == "owner",
                        BusinessUser.is_active.is_(True),
                    )
                )
            ).all()
        )
        if len(owners) != 1:
            raise RuntimeError("notification_active_owner_recipient_ambiguous")
        return business, owners[0].phone

    async def _require_existing_pair(
        self,
        *,
        business_id: int,
        keys: tuple[str, str],
        event_type: str,
        appointment_id: int,
        customer_phone: str,
        expected_payload: dict[str, object],
    ) -> list[int]:
        event_ids: list[int] = []
        for key, recipient_type in zip(
            keys,
            (
                NotificationRecipientType.PATIENT.value,
                NotificationRecipientType.OWNER.value,
            ),
            strict=True,
        ):
            event = await self._repo.get_event_by_idempotency_key(business_id, key)
            if event is None:
                raise NotificationIdempotencyConflictError(
                    "Required committed notification evidence is missing"
                )
            payload = event.payload if isinstance(event.payload, dict) else {}
            expected_phone = customer_phone if recipient_type == "patient" else None
            if (
                event.event_type != event_type
                or event.entity_type != "appointment"
                or event.entity_id != appointment_id
                or event.recipient_type != recipient_type
                or event.channel != NotificationChannel.WHATSAPP.value
                or (expected_phone is not None and event.recipient_phone != expected_phone)
                or (
                    recipient_type == NotificationRecipientType.OWNER.value
                    and payload.get("patient_phone") != customer_phone
                )
                or not event.recipient_phone
                or not payload.get("phone_number_id")
                or any(payload.get(name) != value for name, value in expected_payload.items())
            ):
                raise NotificationIdempotencyConflictError(
                    "Committed notification evidence does not match appointment evidence"
                )
            event_ids.append(event.id)
        return event_ids

    async def verify_appointment_notifications(
        self,
        *,
        business_id: int,
        appointment_id: int,
        customer_phone: str,
        service_name: str,
        resource_name: str,
        start_at: datetime,
        business_timezone: str,
    ) -> list[int]:
        local = start_at.astimezone(ZoneInfo(business_timezone))
        return await self._require_existing_pair(
            business_id=business_id,
            keys=(
                f"appt-confirm-patient-{appointment_id}",
                f"appt-confirm-owner-{appointment_id}",
            ),
            event_type=NotificationEventType.APPOINTMENT_CONFIRMED.value,
            appointment_id=appointment_id,
            customer_phone=customer_phone,
            expected_payload={
                "service": service_name,
                "doctor": resource_name,
                "date": local.strftime("%A, %b %d"),
                "time": local.strftime("%-I:%M %p"),
                "appointment_id": appointment_id,
            },
        )

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
        business, owner_phone = await self._business_and_owner_phone(business_id)
        clinic_name = business.name
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

    async def verify_cancellation_notifications(
        self,
        *,
        business_id: int,
        appointment_id: int,
        customer_phone: str,
        service_name: str,
        resource_name: str,
        start_at: datetime,
        business_timezone: str,
        reason: str | None,
    ) -> list[int]:
        local = start_at.astimezone(ZoneInfo(business_timezone))
        return await self._require_existing_pair(
            business_id=business_id,
            keys=(
                f"appt-cancel-patient-{appointment_id}",
                f"appt-cancel-owner-{appointment_id}",
            ),
            event_type=NotificationEventType.APPOINTMENT_CANCELLED.value,
            appointment_id=appointment_id,
            customer_phone=customer_phone,
            expected_payload={
                "service": service_name,
                "doctor": resource_name,
                "date": local.strftime("%A, %b %d"),
                "time": local.strftime("%-I:%M %p"),
                "reason": reason,
                "appointment_id": appointment_id,
            },
        )

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
        business, owner_phone = await self._business_and_owner_phone(business_id)
        clinic_name = business.name
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

    async def verify_reschedule_notifications(
        self,
        *,
        business_id: int,
        appointment_id: int,
        pending_action_id: int,
        customer_phone: str,
        service_name: str,
        resource_name: str,
        old_start_at: datetime,
        new_start_at: datetime,
        business_timezone: str,
    ) -> list[int]:
        timezone = ZoneInfo(business_timezone)
        old_local = old_start_at.astimezone(timezone)
        new_local = new_start_at.astimezone(timezone)
        return await self._require_existing_pair(
            business_id=business_id,
            keys=(
                f"appt-resched-patient-{appointment_id}-{pending_action_id}",
                f"appt-resched-owner-{appointment_id}-{pending_action_id}",
            ),
            event_type=NotificationEventType.APPOINTMENT_RESCHEDULED.value,
            appointment_id=appointment_id,
            customer_phone=customer_phone,
            expected_payload={
                "service": service_name,
                "doctor": resource_name,
                "old_date": old_local.strftime("%A, %b %d"),
                "old_time": old_local.strftime("%-I:%M %p"),
                "new_date": new_local.strftime("%A, %b %d"),
                "new_time": new_local.strftime("%-I:%M %p"),
                "appointment_id": appointment_id,
            },
        )

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
        business, owner_phone = await self._business_and_owner_phone(business_id)
        clinic_name = business.name
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
