"""Transactional notification service with retention-independent manifest.

Creates immutable notification evidence inside the caller's transaction.
Notification failure rolls back the enclosing savepoint — no silent partial
evidence. Multi-owner recipient resolution uses BusinessUser.

Manifest survives outbox retention cleanup. Replay verifies committed
manifest without reading mutable configuration.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.models.enums import (
    BusinessUserRole,
    NotificationChannel,
    NotificationEventType,
    NotificationRecipientType,
    NotificationStatus,
)
from fonely.models.schema import Business, BusinessUser, NotificationManifest
from fonely.repositories.notifications import NotificationRepository
from fonely.repositories.whatsapp_channels import WhatsAppChannelRepository

logger = logging.getLogger("fonely.services.notifications")


class NotificationConfigurationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class NotificationEvidenceConflictError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class NotificationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    operation: str
    business_id: int
    appointment_id: int
    pending_action_id: int
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


def _canonical_digest(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _snapshot_digest(snapshot: NotificationSnapshot) -> str:
    return _canonical_digest(snapshot.model_dump(mode="json"))


@dataclass(frozen=True)
class ResolvedRecipient:
    recipient_type: str
    phone: str
    name: str | None
    bu_id: int | None


@dataclass(frozen=True)
class NotificationEvidence:
    appointment_result_authoritative: bool
    notification_evidence: str
    event_ids: list[int]


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = NotificationRepository(session)

    async def _resolve_owner_recipients(self, business_id: int) -> list[ResolvedRecipient]:
        """Active OWNER BusinessUsers of a business, deduped by phone.

        Shared by appointment notifications (owner leg) and callback
        notifications (owner-only). Raises rather than silently produce zero
        recipients — a business with no reachable owner cannot be notified, and
        that must be a loud configuration error, not a dropped notification.
        """
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
                    "Cannot create owner notifications."
                ),
            )

        seen_phones: set[str] = set()
        owner_recipients: list[ResolvedRecipient] = []
        for bu in owners:
            normalized = bu.phone.strip()
            if not normalized:
                continue
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

        if not owner_recipients:
            raise NotificationConfigurationError(
                code="no_valid_owner_recipients",
                message=(f"business_id={business_id} has active owners but no valid phones."),
            )

        return owner_recipients

    async def _resolve_recipients(
        self, business_id: int, customer_phone: str, customer_name: str | None
    ) -> list[ResolvedRecipient]:
        patient = ResolvedRecipient(
            recipient_type=NotificationRecipientType.PATIENT.value,
            phone=customer_phone,
            name=customer_name,
            bu_id=None,
        )
        owner_recipients = await self._resolve_owner_recipients(business_id)
        return [patient, *owner_recipients]

    async def _resolve_channel_context(self, business_id: int) -> tuple[str, str]:
        business = await self._session.scalar(select(Business).where(Business.id == business_id))
        clinic_name = business.name if business else "Business"

        phone_number_id = await WhatsAppChannelRepository(self._session).resolve_phone_number_id(
            business_id
        )
        if phone_number_id is None:
            raise NotificationConfigurationError(
                code="whatsapp_mapping_missing",
                message=(
                    f"business_id={business_id} has no active WhatsApp channel. "
                    "Register one in business_whatsapp_channels."
                ),
            )
        return clinic_name, phone_number_id

    def _build_snapshot(
        self,
        *,
        operation: str,
        business_id: int,
        appointment_id: int,
        pending_action_id: int,
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
    ) -> NotificationSnapshot:
        price_str = format(Decimal(str(price)), "f") if price is not None else None

        return NotificationSnapshot(
            operation=operation,
            business_id=business_id,
            appointment_id=appointment_id,
            pending_action_id=pending_action_id,
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
        )

    def _snapshot_to_payload(self, snapshot: NotificationSnapshot) -> dict[str, Any]:
        from zoneinfo import ZoneInfo

        payload: dict[str, Any] = {
            "appointment_id": snapshot.appointment_id,
            "phone_number_id": snapshot.phone_number_id,
            "equivalence_snapshot": snapshot.model_dump(mode="json"),
            "equivalence_digest": _snapshot_digest(snapshot),
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
                tz = ZoneInfo(snapshot.business_timezone)
                old_dt = datetime.fromisoformat(snapshot.old_start_at).astimezone(tz)
                new_dt = datetime.fromisoformat(snapshot.new_start_at).astimezone(tz)
                payload["old_time"] = old_dt.strftime("%-I:%M %p")
                payload["new_time"] = new_dt.strftime("%-I:%M %p")
                payload["old_date"] = old_dt.strftime("%A, %b %d")
                payload["new_date"] = new_dt.strftime("%A, %b %d")
        else:
            payload["patient_name"] = snapshot.patient_name
            payload["patient_phone"] = snapshot.patient_phone
            payload["service"] = snapshot.service_name
            payload["doctor"] = snapshot.resource_name
            if snapshot.reason:
                payload["reason"] = snapshot.reason
            if snapshot.old_start_at and snapshot.new_start_at:
                tz = ZoneInfo(snapshot.business_timezone)
                old_dt = datetime.fromisoformat(snapshot.old_start_at).astimezone(tz)
                new_dt = datetime.fromisoformat(snapshot.new_start_at).astimezone(tz)
                payload["old_time"] = old_dt.strftime("%-I:%M %p")
                payload["new_time"] = new_dt.strftime("%-I:%M %p")
                payload["old_date"] = old_dt.strftime("%A, %b %d")
                payload["new_date"] = new_dt.strftime("%A, %b %d")

        return payload

    def _idempotency_key(
        self,
        operation: str,
        appointment_id: int,
        pending_action_id: int,
        recipient: ResolvedRecipient,
    ) -> str:
        if recipient.bu_id is not None:
            return (
                f"notif-{operation}-owner-{appointment_id}"
                f"-bu{recipient.bu_id}-pa{pending_action_id}"
            )
        return f"notif-{operation}-patient-{appointment_id}-pa{pending_action_id}"

    def _event_type_for_operation(self, operation: str) -> str:
        return {
            "create": NotificationEventType.APPOINTMENT_CONFIRMED.value,
            "cancel": NotificationEventType.APPOINTMENT_CANCELLED.value,
            "reschedule": NotificationEventType.APPOINTMENT_RESCHEDULED.value,
        }[operation]

    def _build_manifest_digest(
        self,
        *,
        schema_version: int,
        business_id: int,
        entity_type: str,
        entity_id: int,
        operation: str,
        pending_action_id: int,
        actor_kind: str,
        actor_phone: str | None,
        actor_bu_id: int | None,
        channel: str,
        phone_number_id: str,
        recipients: list[dict[str, Any]],
    ) -> str:
        digest_input = {
            "schema_version": schema_version,
            "business_id": business_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "operation": operation,
            "pending_action_id": pending_action_id,
            "actor_kind": actor_kind,
            "actor_phone": actor_phone,
            "actor_bu_id": actor_bu_id,
            "channel": channel,
            "phone_number_id": phone_number_id,
            "recipients": [
                {
                    "recipient_type": r["recipient_type"],
                    "phone_e164": r["phone_e164"],
                    "bu_id": r["bu_id"],
                    "idempotency_key": r["idempotency_key"],
                    "digest": r["digest"],
                }
                for r in recipients
            ],
        }
        return _canonical_digest(digest_input)

    async def _create_events_with_manifest(
        self,
        *,
        operation: str,
        business_id: int,
        appointment_id: int,
        pending_action_id: int,
        actor_kind: str,
        actor_phone: str | None,
        actor_bu_id: int | None,
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
    ) -> list[int]:
        recipients = await self._resolve_recipients(business_id, customer_phone, customer_name)
        clinic_name, phone_number_id = await self._resolve_channel_context(business_id)

        event_type = self._event_type_for_operation(operation)
        event_ids: list[int] = []
        manifest_entries: list[dict[str, Any]] = []

        for recipient in recipients:
            snapshot = self._build_snapshot(
                operation=operation,
                business_id=business_id,
                appointment_id=appointment_id,
                pending_action_id=pending_action_id,
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
            )

            payload = self._snapshot_to_payload(snapshot)
            key = self._idempotency_key(operation, appointment_id, pending_action_id, recipient)

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

            event_id = event.id if event is not None else None
            if event is not None:
                event_ids.append(event.id)

            manifest_entries.append(
                {
                    "recipient_type": recipient.recipient_type,
                    "phone_e164": recipient.phone,
                    "name": recipient.name,
                    "bu_id": recipient.bu_id,
                    "idempotency_key": key,
                    "outbox_event_id": event_id,
                    "snapshot": snapshot.model_dump(mode="json"),
                    "digest": _snapshot_digest(snapshot),
                }
            )

        root_digest = self._build_manifest_digest(
            schema_version=1,
            business_id=business_id,
            entity_type="appointment",
            entity_id=appointment_id,
            operation=operation,
            pending_action_id=pending_action_id,
            actor_kind=actor_kind,
            actor_phone=actor_phone,
            actor_bu_id=actor_bu_id,
            channel=NotificationChannel.WHATSAPP.value,
            phone_number_id=phone_number_id,
            recipients=manifest_entries,
        )

        stmt = (
            pg_insert(NotificationManifest)
            .values(
                business_id=business_id,
                entity_type="appointment",
                entity_id=appointment_id,
                operation=operation,
                pending_action_id=pending_action_id,
                actor_kind=actor_kind,
                actor_phone=actor_phone,
                actor_bu_id=actor_bu_id,
                recipient_count=len(manifest_entries),
                recipient_manifest=manifest_entries,
                channel=NotificationChannel.WHATSAPP.value,
                phone_number_id=phone_number_id,
                equivalence_digest=root_digest,
                schema_version=1,
                outbox_event_ids=[
                    e for e in (m["outbox_event_id"] for m in manifest_entries) if e is not None
                ],
            )
            .on_conflict_do_nothing(index_elements=["business_id", "pending_action_id"])
        )
        await self._session.execute(stmt)
        await self._session.flush()

        return event_ids

    async def create_appointment_notifications(
        self,
        business_id: int,
        appointment_id: int,
        pending_action_id: int,
        customer_phone: str,
        customer_name: str | None,
        service_name: str,
        resource_name: str,
        start_at: datetime,
        price: Any | None,
        business_timezone: str,
        actor_kind: str = "customer",
        actor_phone: str | None = None,
        actor_bu_id: int | None = None,
    ) -> list[int]:
        return await self._create_events_with_manifest(
            operation="create",
            business_id=business_id,
            appointment_id=appointment_id,
            pending_action_id=pending_action_id,
            actor_kind=actor_kind,
            actor_phone=actor_phone,
            actor_bu_id=actor_bu_id,
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
        pending_action_id: int,
        customer_phone: str,
        customer_name: str | None,
        service_name: str,
        resource_name: str,
        start_at: datetime,
        business_timezone: str,
        reason: str | None = None,
        actor_kind: str = "customer",
        actor_phone: str | None = None,
        actor_bu_id: int | None = None,
    ) -> list[int]:
        return await self._create_events_with_manifest(
            operation="cancel",
            business_id=business_id,
            appointment_id=appointment_id,
            pending_action_id=pending_action_id,
            actor_kind=actor_kind,
            actor_phone=actor_phone,
            actor_bu_id=actor_bu_id,
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
        actor_kind: str = "customer",
        actor_phone: str | None = None,
        actor_bu_id: int | None = None,
    ) -> list[int]:
        return await self._create_events_with_manifest(
            operation="reschedule",
            business_id=business_id,
            appointment_id=appointment_id,
            pending_action_id=pending_action_id,
            actor_kind=actor_kind,
            actor_phone=actor_phone,
            actor_bu_id=actor_bu_id,
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            old_start_at=old_start_at,
            new_start_at=new_start_at,
            business_timezone=business_timezone,
        )

    async def create_callback_notification(
        self,
        *,
        business_id: int,
        callback_pending_action_id: int,
        caller_phone: str,
        reason_code: str,
        service_name: str | None = None,
        target_date: str | None = None,
        attempted_candidates: list[str] | None = None,
    ) -> list[int]:
        """Push an OWNER WhatsApp notification that a caller needs a call-back.

        Emitted when #36 persists a callback pending action on a voice give-up.
        Notifies the OWNER only (a callback is a follow-up the clinic owes the
        caller, not something the caller is told), carrying the partial booking
        facts the owner needs to complete the booking by phone.

        DELIBERATELY NO APPOINTMENT MANIFEST — this is not create_*_notifications'
        manifest path, and that is on purpose, not an oversight:
          * A callback has NO appointment_id; the manifest machinery
            (_create_events_with_manifest, _build_snapshot, the manifest table)
            is appointment-scoped throughout — keyed on appointment_id, snapshot
            carries appointment_id. Routing a callback through it would FABRICATE
            appointment semantics for a thing that is not a booking.
          * The manifest exists to give BOOKING notifications retention-independent
            proof (they must outlive the appointment's 365-day retention). A
            "call them back" nudge is not a booking and does not need booking-grade
            evidence — the outbox row plus the worker's provider-attempt evidence
            is the right durability level for a nudge.
        A future dev must NOT "fix" this by manifest-wrapping callbacks — appointments
        get manifests because they are bookings; callbacks deliberately do not.

        Idempotent per (callback, owner): re-emitting for the same callback does
        not duplicate rows. Reuses the existing owner resolution, channel-context
        resolution, and outbox→worker→sender delivery pipeline entirely.
        """
        owners = await self._resolve_owner_recipients(business_id)
        clinic_name, phone_number_id = await self._resolve_channel_context(business_id)

        payload_facts: dict[str, Any] = {
            "phone_number_id": phone_number_id,
            "clinic_name": clinic_name,
            "caller_phone": caller_phone,
            "reason_code": reason_code,
            "service_name": service_name,
            "target_date": target_date,
            "attempted_candidates": list(attempted_candidates or []),
        }

        event_ids: list[int] = []
        for owner in owners:
            key = f"callback-notify-owner-{callback_pending_action_id}-{owner.bu_id}"
            event = await self._repo.insert_event_idempotent(
                {
                    "business_id": business_id,
                    "event_type": NotificationEventType.CALLBACK_REQUESTED.value,
                    # NOT 'appointment' — a callback references a pending_action.
                    "entity_type": "pending_action",
                    "entity_id": callback_pending_action_id,
                    "recipient_type": owner.recipient_type,
                    "recipient_phone": owner.phone,
                    "recipient_name": owner.name,
                    "channel": NotificationChannel.WHATSAPP.value,
                    "payload": payload_facts,
                    "status": NotificationStatus.PENDING.value,
                    "idempotency_key": key,
                }
            )
            if event is not None:
                event_ids.append(event.id)
        return event_ids

    async def verify_committed_notifications(
        self,
        business_id: int,
        appointment_id: int,
        operation: str,
        pending_action_id: int | None = None,
    ) -> NotificationEvidence:
        filters = [
            NotificationManifest.business_id == business_id,
            NotificationManifest.entity_type == "appointment",
            NotificationManifest.entity_id == appointment_id,
            NotificationManifest.operation == operation,
        ]
        if pending_action_id is not None:
            filters.append(NotificationManifest.pending_action_id == pending_action_id)
        manifest = (
            await self._session.scalars(select(NotificationManifest).where(*filters))
        ).first()

        if manifest is not None:
            entries = manifest.recipient_manifest or []

            for entry in entries:
                snapshot_data = entry.get("snapshot")
                stored_digest = entry.get("digest")
                if snapshot_data is None or stored_digest is None:
                    raise NotificationEvidenceConflictError(
                        code="manifest_corrupted",
                        message=f"Manifest {manifest.id} missing snapshot/digest",
                    )
                recomputed = _canonical_digest(snapshot_data)
                if recomputed != stored_digest:
                    raise NotificationEvidenceConflictError(
                        code="manifest_corrupted",
                        message=(
                            f"Manifest {manifest.id} recipient "
                            f"{entry.get('recipient_type')} digest mismatch"
                        ),
                    )

            digest_input_recipients = [
                {
                    "recipient_type": e["recipient_type"],
                    "phone_e164": e["phone_e164"],
                    "bu_id": e["bu_id"],
                    "idempotency_key": e["idempotency_key"],
                    "digest": e["digest"],
                }
                for e in entries
            ]
            expected_digest = self._build_manifest_digest(
                schema_version=manifest.schema_version,
                business_id=manifest.business_id,
                entity_type=manifest.entity_type,
                entity_id=manifest.entity_id,
                operation=manifest.operation,
                pending_action_id=manifest.pending_action_id,
                actor_kind=manifest.actor_kind,
                actor_phone=manifest.actor_phone,
                actor_bu_id=manifest.actor_bu_id,
                channel=manifest.channel,
                phone_number_id=manifest.phone_number_id,
                recipients=digest_input_recipients,
            )
            if expected_digest != manifest.equivalence_digest:
                raise NotificationEvidenceConflictError(
                    code="manifest_corrupted",
                    message=f"Manifest {manifest.id} root digest mismatch",
                )
            if manifest.recipient_count != len(entries):
                raise NotificationEvidenceConflictError(
                    code="manifest_corrupted",
                    message=f"Manifest {manifest.id} recipient count mismatch",
                )

            archival_ids = list(manifest.outbox_event_ids or [])
            if archival_ids:
                live = await self._repo.get_events_for_entity(
                    business_id, "appointment", appointment_id
                )
                live_ids = {e.id for e in live}
                has_live = any(eid in live_ids for eid in archival_ids)
            else:
                has_live = False

            evidence_status = "verified" if has_live else "verified_delivery_unknown"
            return NotificationEvidence(
                appointment_result_authoritative=True,
                notification_evidence=evidence_status,
                event_ids=archival_ids,
            )

        event_type = self._event_type_for_operation(operation)
        existing = await self._repo.get_events_for_entity(
            business_id, "appointment", appointment_id
        )
        matching = [e for e in existing if e.event_type == event_type]

        if not matching:
            logger.warning(
                "legacy_notification_irrecoverable: business_id=%d entity_id=%d op=%s",
                business_id,
                appointment_id,
                operation,
            )
            return NotificationEvidence(
                appointment_result_authoritative=True,
                notification_evidence="irrecoverable",
                event_ids=[],
            )

        has_v1 = any((e.payload or {}).get("equivalence_snapshot") is not None for e in matching)
        if has_v1:
            raise NotificationEvidenceConflictError(
                code="missing_manifest_with_v1_outbox",
                message=(
                    f"v1 outbox events exist for appointment {appointment_id} "
                    f"operation {operation} but no manifest found"
                ),
            )

        logger.warning(
            "legacy_notification_unmanifested: business_id=%d entity_id=%d op=%s count=%d",
            business_id,
            appointment_id,
            operation,
            len(matching),
        )
        return NotificationEvidence(
            appointment_result_authoritative=True,
            notification_evidence="unverifiable",
            event_ids=[e.id for e in matching],
        )

    async def verify_appointment_notifications(
        self,
        business_id: int,
        appointment_id: int,
        pending_action_id: int | None = None,
    ) -> NotificationEvidence:
        return await self.verify_committed_notifications(
            business_id, appointment_id, "create", pending_action_id
        )

    async def verify_cancellation_notifications(
        self,
        business_id: int,
        appointment_id: int,
        pending_action_id: int | None = None,
    ) -> NotificationEvidence:
        return await self.verify_committed_notifications(
            business_id, appointment_id, "cancel", pending_action_id
        )

    async def verify_reschedule_notifications(
        self,
        business_id: int,
        appointment_id: int,
        pending_action_id: int | None = None,
    ) -> NotificationEvidence:
        return await self.verify_committed_notifications(
            business_id, appointment_id, "reschedule", pending_action_id
        )
