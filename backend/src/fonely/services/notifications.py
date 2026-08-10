"""Transactional notification outbox service.

Creates immutable notification evidence inside the caller's transaction.
Notification failure rolls back the enclosing savepoint — no silent partial
evidence. Multi-owner recipient resolution uses BusinessUser, not
Business.primary_contact_phone.

Replay returns exact committed evidence without reading mutable configuration.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.config import settings
from fonely.models.enums import (
    BusinessUserRole,
    NotificationChannel,
    NotificationEventType,
    NotificationRecipientType,
    NotificationStatus,
)
from fonely.models.schema import Business, BusinessUser
from fonely.repositories.notifications import NotificationRepository
from fonely.services.whatsapp_config import WhatsAppBusinessMapping

logger = logging.getLogger("fonely.services.notifications")


class NotificationConfigurationError(Exception):
    """Raised before any mutation when notification prerequisites are unmet."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class NotificationEvidenceConflictError(Exception):
    """Raised when committed evidence does not match expected replay."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class NotificationSnapshot(BaseModel):
    """Immutable evidence embedded in the outbox event payload."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    operation: str
    business_id: int
    appointment_id: int
    recipient_type: str
    recipient_phone: str
    recipient_bu_id: int | None = None
    clinic_name: str
    patient_phone: str
    patient_name: str | None = None
    service_name: str
    resource_name: str
    business_timezone: str
    start_at: str | None = None
    old_start_at: str | None = None
    new_start_at: str | None = None
    price: str | None = None
    reason: str | None = None
    phone_number_id: str
    pending_action_id: int | None = None


def _canonical_digest(snapshot: NotificationSnapshot) -> str:
    canonical = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ResolvedRecipient:
    recipient_type: str
    phone: str
    name: str | None
    bu_id: int | None


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = NotificationRepository(session)

    async def _resolve_recipients(
        self, business_id: int, customer_phone: str, customer_name: str | None
    ) -> list[ResolvedRecipient]:
        patient = ResolvedRecipient(
            recipient_type=NotificationRecipientType.PATIENT.value,
            phone=customer_phone,
            name=customer_name,
            bu_id=None,
        )

        owners = (
            await self._session.scalars(
                select(BusinessUser)
                .where(
                    BusinessUser.business_id == business_id,
                    BusinessUser.role == BusinessUserRole.OWNER.value,
                    BusinessUser.is_active.is_(True),
                )
                .order_by(BusinessUser.id)
            )
        ).all()

        if not owners:
            raise NotificationConfigurationError(
                code="no_valid_owner_recipients",
                message=(
                    f"business_id={business_id} has no active owner recipients. "
                    "Cannot create appointment notifications."
                ),
            )

        seen_phones: set[str] = set()
        owner_recipients: list[ResolvedRecipient] = []
        for bu in owners:
            normalized = bu.phone.strip()
            if normalized in seen_phones:
                continue
            seen_phones.add(normalized)
            owner_recipients.append(
                ResolvedRecipient(
                    recipient_type=NotificationRecipientType.OWNER.value,
                    phone=normalized,
                    name=None,
                    bu_id=bu.id,
                )
            )

        return [patient, *owner_recipients]

    async def _resolve_channel_context(self, business_id: int) -> tuple[str, str]:
        business = await self._session.scalar(select(Business).where(Business.id == business_id))
        clinic_name = business.name if business else "Business"

        phone_number_id = WhatsAppBusinessMapping().get_phone_number_id(
            business_id, preferred=settings.whatsapp_phone_number_id or None
        )
        if phone_number_id is None:
            raise NotificationConfigurationError(
                code="whatsapp_mapping_missing",
                message=f"No WhatsApp mapping for business_id={business_id}",
            )
        return clinic_name, phone_number_id

    def _build_snapshot(
        self,
        *,
        operation: str,
        business_id: int,
        appointment_id: int,
        recipient: ResolvedRecipient,
        clinic_name: str,
        customer_phone: str,
        customer_name: str | None,
        service_name: str,
        resource_name: str,
        business_timezone: str,
        phone_number_id: str,
        start_at: datetime | None = None,
        old_start_at: datetime | None = None,
        new_start_at: datetime | None = None,
        price: Any | None = None,
        reason: str | None = None,
        pending_action_id: int | None = None,
    ) -> NotificationSnapshot:
        from decimal import Decimal as _Decimal

        price_str = str(_Decimal(str(price)).normalize()) if price is not None else None

        return NotificationSnapshot(
            operation=operation,
            business_id=business_id,
            appointment_id=appointment_id,
            recipient_type=recipient.recipient_type,
            recipient_phone=recipient.phone,
            recipient_bu_id=recipient.bu_id,
            clinic_name=clinic_name,
            patient_phone=customer_phone,
            patient_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            phone_number_id=phone_number_id,
            start_at=start_at.isoformat() if start_at else None,
            old_start_at=old_start_at.isoformat() if old_start_at else None,
            new_start_at=new_start_at.isoformat() if new_start_at else None,
            price=price_str,
            reason=reason,
            pending_action_id=pending_action_id,
        )

    def _snapshot_to_payload(self, snapshot: NotificationSnapshot) -> dict[str, Any]:
        from zoneinfo import ZoneInfo

        payload: dict[str, Any] = {
            "appointment_id": snapshot.appointment_id,
            "phone_number_id": snapshot.phone_number_id,
            "equivalence_snapshot": snapshot.model_dump(mode="json"),
            "equivalence_digest": _canonical_digest(snapshot),
            "schema_version": snapshot.schema_version,
        }

        if snapshot.start_at:
            dt = datetime.fromisoformat(snapshot.start_at)
            local_time = dt.astimezone(ZoneInfo(snapshot.business_timezone))
            payload["date"] = local_time.strftime("%A, %b %d")
            payload["time"] = local_time.strftime("%-I:%M %p")

        if snapshot.recipient_type == NotificationRecipientType.PATIENT.value:
            payload["clinic_name"] = snapshot.clinic_name
            payload["service"] = snapshot.service_name
            payload["doctor"] = snapshot.resource_name
            if snapshot.price:
                payload["price"] = f"₹{snapshot.price}"
            if snapshot.reason:
                payload["reason"] = snapshot.reason
            if snapshot.old_start_at and snapshot.new_start_at:
                old_dt = datetime.fromisoformat(snapshot.old_start_at)
                new_dt = datetime.fromisoformat(snapshot.new_start_at)
                tz = ZoneInfo(snapshot.business_timezone)
                payload["old_time"] = old_dt.astimezone(tz).strftime("%-I:%M %p")
                payload["new_time"] = new_dt.astimezone(tz).strftime("%-I:%M %p")
                payload["old_date"] = old_dt.astimezone(tz).strftime("%A, %b %d")
                payload["new_date"] = new_dt.astimezone(tz).strftime("%A, %b %d")
        else:
            payload["patient_name"] = snapshot.patient_name
            payload["patient_phone"] = snapshot.patient_phone
            payload["service"] = snapshot.service_name
            payload["doctor"] = snapshot.resource_name
            if snapshot.reason:
                payload["reason"] = snapshot.reason
            if snapshot.old_start_at and snapshot.new_start_at:
                old_dt = datetime.fromisoformat(snapshot.old_start_at)
                new_dt = datetime.fromisoformat(snapshot.new_start_at)
                tz = ZoneInfo(snapshot.business_timezone)
                payload["old_time"] = old_dt.astimezone(tz).strftime("%-I:%M %p")
                payload["new_time"] = new_dt.astimezone(tz).strftime("%-I:%M %p")
                payload["old_date"] = old_dt.astimezone(tz).strftime("%A, %b %d")
                payload["new_date"] = new_dt.astimezone(tz).strftime("%A, %b %d")

        return payload

    def _idempotency_key(
        self, operation: str, appointment_id: int, recipient: ResolvedRecipient
    ) -> str:
        if recipient.bu_id is not None:
            return f"notif-{operation}-owner-{appointment_id}-bu{recipient.bu_id}"
        return f"notif-{operation}-patient-{appointment_id}"

    def _event_type_for_operation(self, operation: str) -> str:
        return {
            "create": NotificationEventType.APPOINTMENT_CONFIRMED.value,
            "cancel": NotificationEventType.APPOINTMENT_CANCELLED.value,
            "reschedule": NotificationEventType.APPOINTMENT_RESCHEDULED.value,
        }[operation]

    async def _create_events(
        self,
        *,
        operation: str,
        business_id: int,
        appointment_id: int,
        customer_phone: str,
        customer_name: str | None,
        service_name: str,
        resource_name: str,
        business_timezone: str,
        start_at: datetime | None = None,
        old_start_at: datetime | None = None,
        new_start_at: datetime | None = None,
        price: Any | None = None,
        reason: str | None = None,
        pending_action_id: int | None = None,
    ) -> list[int]:
        recipients = await self._resolve_recipients(business_id, customer_phone, customer_name)
        clinic_name, phone_number_id = await self._resolve_channel_context(business_id)

        event_type = self._event_type_for_operation(operation)
        event_ids: list[int] = []

        for recipient in recipients:
            snapshot = self._build_snapshot(
                operation=operation,
                business_id=business_id,
                appointment_id=appointment_id,
                recipient=recipient,
                clinic_name=clinic_name,
                customer_phone=customer_phone,
                customer_name=customer_name,
                service_name=service_name,
                resource_name=resource_name,
                business_timezone=business_timezone,
                phone_number_id=phone_number_id,
                start_at=start_at,
                old_start_at=old_start_at,
                new_start_at=new_start_at,
                price=price,
                reason=reason,
                pending_action_id=pending_action_id,
            )

            payload = self._snapshot_to_payload(snapshot)
            key = self._idempotency_key(operation, appointment_id, recipient)

            event = await self._repo.insert_event_idempotent(
                {
                    "business_id": business_id,
                    "event_type": event_type,
                    "entity_type": "appointment",
                    "entity_id": appointment_id,
                    "recipient_type": recipient.recipient_type,
                    "recipient_phone": recipient.phone,
                    "recipient_name": recipient.name,
                    "channel": NotificationChannel.WHATSAPP.value,
                    "payload": payload,
                    "status": NotificationStatus.PENDING.value,
                    "idempotency_key": key,
                }
            )
            if event is not None:
                event_ids.append(event.id)

        return event_ids

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
        return await self._create_events(
            operation="create",
            business_id=business_id,
            appointment_id=appointment_id,
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            start_at=start_at,
            price=price,
            business_timezone=business_timezone,
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
        return await self._create_events(
            operation="cancel",
            business_id=business_id,
            appointment_id=appointment_id,
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            start_at=start_at,
            reason=reason,
            business_timezone=business_timezone,
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
        return await self._create_events(
            operation="reschedule",
            business_id=business_id,
            appointment_id=appointment_id,
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            old_start_at=old_start_at,
            new_start_at=new_start_at,
            pending_action_id=pending_action_id,
            business_timezone=business_timezone,
        )

    async def verify_committed_notifications(
        self,
        business_id: int,
        appointment_id: int,
        operation: str,
    ) -> list[int]:
        existing = await self._repo.get_events_for_entity(
            business_id, "appointment", appointment_id
        )

        event_type = self._event_type_for_operation(operation)
        matching = [e for e in existing if e.event_type == event_type]

        if not matching:
            raise NotificationEvidenceConflictError(
                code="missing_evidence",
                message=(
                    f"No committed {operation} notification evidence for "
                    f"appointment {appointment_id}"
                ),
            )

        for event in matching:
            payload = event.payload or {}
            snapshot_data = payload.get("equivalence_snapshot")
            digest = payload.get("equivalence_digest")

            if snapshot_data is not None and digest is not None:
                try:
                    reconstructed = NotificationSnapshot(**snapshot_data)
                    expected_digest = _canonical_digest(reconstructed)
                    if expected_digest != digest:
                        raise NotificationEvidenceConflictError(
                            code="digest_mismatch",
                            message=(
                                f"Notification {event.id} digest mismatch: "
                                f"stored={digest[:16]}... computed={expected_digest[:16]}..."
                            ),
                        )
                except NotificationEvidenceConflictError:
                    raise
                except Exception as exc:
                    raise NotificationEvidenceConflictError(
                        code="corrupted_snapshot",
                        message=f"Notification {event.id} snapshot parse error: {exc}",
                    ) from exc
            else:
                logger.info(
                    "legacy_notification_evidence: event_id=%d operation=%s "
                    "has_snapshot=%s has_digest=%s",
                    event.id,
                    operation,
                    snapshot_data is not None,
                    digest is not None,
                )

        return [e.id for e in matching]

    async def verify_appointment_notifications(
        self,
        business_id: int,
        appointment_id: int,
    ) -> list[int]:
        return await self.verify_committed_notifications(business_id, appointment_id, "create")

    async def verify_cancellation_notifications(
        self,
        business_id: int,
        appointment_id: int,
    ) -> list[int]:
        return await self.verify_committed_notifications(business_id, appointment_id, "cancel")

    async def verify_reschedule_notifications(
        self,
        business_id: int,
        appointment_id: int,
    ) -> list[int]:
        return await self.verify_committed_notifications(business_id, appointment_id, "reschedule")
