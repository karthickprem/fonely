"""Notification outbox service — creates events inside the caller's transaction."""

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.config import settings
from fonely.domain.appointments.datetimes import instant
from fonely.models.enums import (
    NotificationChannel,
    NotificationEventType,
    NotificationRecipientType,
    NotificationStatus,
)
from fonely.models.schema import Business, BusinessUser, NotificationOutboxEvent
from fonely.repositories.notifications import NotificationRepository
from fonely.services.whatsapp_config import WhatsAppBusinessMapping


class NotificationIdempotencyConflictError(RuntimeError):
    pass


class NotificationPairSnapshot(BaseModel):
    """Versioned immutable facts sufficient to verify or repair one event pair."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    operation: Literal["create", "cancel", "reschedule"]
    business_id: int = Field(gt=0)
    appointment_id: int = Field(gt=0)
    pending_action_id: int | None = Field(default=None, gt=0)
    clinic_name: str = Field(min_length=1, max_length=200)
    patient_phone: str = Field(min_length=1, max_length=20)
    patient_name: str | None = Field(default=None, max_length=200)
    owner_phone: str = Field(min_length=1, max_length=20)
    phone_number_id: str = Field(min_length=1, max_length=100)
    service_name: str = Field(min_length=1, max_length=200)
    resource_name: str = Field(min_length=1, max_length=200)
    business_timezone: str = Field(min_length=1, max_length=50)
    start_at: AwareDatetime | None = None
    old_start_at: AwareDatetime | None = None
    new_start_at: AwareDatetime | None = None
    price: str | None = None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "NotificationPairSnapshot":
        if self.operation in {"create", "cancel"} and self.start_at is None:
            raise ValueError("create/cancel notification requires start_at")
        if self.operation == "reschedule" and (
            self.pending_action_id is None or self.old_start_at is None or self.new_start_at is None
        ):
            raise ValueError("reschedule notification requires operation identity and times")
        return self


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = NotificationRepository(session)

    @staticmethod
    def _canonical_digest(value: object) -> str:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _snapshot_digest(snapshot: NotificationPairSnapshot) -> str:
        return NotificationService._canonical_digest(snapshot.model_dump(mode="json"))

    async def _insert_or_verify(self, values: dict[str, Any]) -> int:
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
        if not self._event_equivalent(existing, values):
            raise NotificationIdempotencyConflictError(
                "Notification idempotency key exists with non-equivalent durable evidence"
            )
        return existing.id

    def _event_equivalent(
        self, existing: NotificationOutboxEvent, expected: dict[str, Any]
    ) -> bool:
        return (
            existing.business_id == expected["business_id"]
            and existing.event_type == expected["event_type"]
            and existing.recipient_type == expected["recipient_type"]
            and existing.recipient_phone == expected["recipient_phone"]
            and existing.recipient_name == expected.get("recipient_name")
            and existing.channel == expected["channel"]
            and existing.entity_type == expected["entity_type"]
            and existing.entity_id == expected["entity_id"]
            and self._canonical_digest(existing.payload)
            == self._canonical_digest(expected["payload"])
            and existing.status
            in {
                NotificationStatus.PENDING.value,
                NotificationStatus.PROCESSING.value,
                NotificationStatus.DELIVERED.value,
                NotificationStatus.FAILED.value,
                NotificationStatus.DEAD_LETTER.value,
                NotificationStatus.UNKNOWN.value,
            }
        )

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

    @staticmethod
    def _keys(snapshot: NotificationPairSnapshot) -> tuple[str, str]:
        if snapshot.operation == "create":
            prefix = f"appt-confirm-{{recipient}}-{snapshot.appointment_id}"
        elif snapshot.operation == "cancel":
            prefix = f"appt-cancel-{{recipient}}-{snapshot.appointment_id}"
        else:
            assert snapshot.pending_action_id is not None
            prefix = (
                f"appt-resched-{{recipient}}-{snapshot.appointment_id}-{snapshot.pending_action_id}"
            )
        return prefix.format(recipient="patient"), prefix.format(recipient="owner")

    @staticmethod
    def _event_type(snapshot: NotificationPairSnapshot) -> str:
        return {
            "create": NotificationEventType.APPOINTMENT_CONFIRMED.value,
            "cancel": NotificationEventType.APPOINTMENT_CANCELLED.value,
            "reschedule": NotificationEventType.APPOINTMENT_RESCHEDULED.value,
        }[snapshot.operation]

    def _payloads(
        self, snapshot: NotificationPairSnapshot
    ) -> tuple[dict[str, object], dict[str, object]]:
        timezone = ZoneInfo(snapshot.business_timezone)
        common: dict[str, object] = {
            "schema_version": 1,
            "template_type": self._event_type(snapshot),
            "clinic_name": snapshot.clinic_name,
            "service": snapshot.service_name,
            "doctor": snapshot.resource_name,
            "appointment_id": snapshot.appointment_id,
            "phone_number_id": snapshot.phone_number_id,
        }
        if snapshot.operation in {"create", "cancel"}:
            assert snapshot.start_at is not None
            local = snapshot.start_at.astimezone(timezone)
            common.update(
                date=local.strftime("%A, %b %d"),
                time=local.strftime("%-I:%M %p"),
            )
        if snapshot.operation == "create":
            common["price"] = f"₹{snapshot.price}" if snapshot.price else None
        elif snapshot.operation == "cancel":
            common["reason"] = snapshot.reason
        else:
            assert snapshot.old_start_at is not None
            assert snapshot.new_start_at is not None
            old_local = snapshot.old_start_at.astimezone(timezone)
            new_local = snapshot.new_start_at.astimezone(timezone)
            common.update(
                old_date=old_local.strftime("%A, %b %d"),
                old_time=old_local.strftime("%-I:%M %p"),
                new_date=new_local.strftime("%A, %b %d"),
                new_time=new_local.strftime("%-I:%M %p"),
            )
        snapshot_json = snapshot.model_dump(mode="json")
        common["equivalence_snapshot"] = snapshot_json
        common["equivalence_digest"] = self._snapshot_digest(snapshot)

        patient = dict(common)
        owner = dict(common)
        owner.update(
            patient_name=snapshot.patient_name,
            patient_phone=snapshot.patient_phone,
        )
        return patient, owner

    def _event_values(
        self, snapshot: NotificationPairSnapshot
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        patient_key, owner_key = self._keys(snapshot)
        patient_payload, owner_payload = self._payloads(snapshot)
        common = {
            "business_id": snapshot.business_id,
            "event_type": self._event_type(snapshot),
            "entity_type": "appointment",
            "entity_id": snapshot.appointment_id,
            "channel": NotificationChannel.WHATSAPP.value,
            "status": NotificationStatus.PENDING.value,
        }
        return (
            {
                **common,
                "recipient_type": NotificationRecipientType.PATIENT.value,
                "recipient_phone": snapshot.patient_phone,
                "recipient_name": snapshot.patient_name,
                "payload": patient_payload,
                "idempotency_key": patient_key,
            },
            {
                **common,
                "recipient_type": NotificationRecipientType.OWNER.value,
                "recipient_phone": snapshot.owner_phone,
                "recipient_name": None,
                "payload": owner_payload,
                "idempotency_key": owner_key,
            },
        )

    async def _create_or_verify_pair(self, snapshot: NotificationPairSnapshot) -> list[int]:
        patient, owner = self._event_values(snapshot)
        async with self._session.begin_nested():
            return [
                await self._insert_or_verify(patient),
                await self._insert_or_verify(owner),
            ]

    @staticmethod
    def _event_format(event: NotificationOutboxEvent) -> Literal["v1", "legacy"]:
        if not isinstance(event.payload, dict):
            raise NotificationIdempotencyConflictError(
                "Committed notification payload is malformed"
            )
        has_snapshot = "equivalence_snapshot" in event.payload
        has_digest = "equivalence_digest" in event.payload
        if has_snapshot != has_digest:
            raise NotificationIdempotencyConflictError(
                "Committed notification format is partially versioned"
            )
        return "v1" if has_snapshot else "legacy"

    @staticmethod
    def _valid_status(event: NotificationOutboxEvent) -> bool:
        return event.status in {
            NotificationStatus.PENDING.value,
            NotificationStatus.PROCESSING.value,
            NotificationStatus.DELIVERED.value,
            NotificationStatus.FAILED.value,
            NotificationStatus.DEAD_LETTER.value,
            NotificationStatus.UNKNOWN.value,
        }

    def _legacy_payloads(
        self, snapshot: NotificationPairSnapshot
    ) -> tuple[dict[str, object], dict[str, object]]:
        timezone = ZoneInfo(snapshot.business_timezone)
        patient: dict[str, object] = {
            "clinic_name": snapshot.clinic_name,
            "service": snapshot.service_name,
            "doctor": snapshot.resource_name,
            "appointment_id": snapshot.appointment_id,
            "phone_number_id": snapshot.phone_number_id,
        }
        owner: dict[str, object] = {
            "patient_name": snapshot.patient_name,
            "patient_phone": snapshot.patient_phone,
            "service": snapshot.service_name,
            "doctor": snapshot.resource_name,
            "appointment_id": snapshot.appointment_id,
            "phone_number_id": snapshot.phone_number_id,
        }
        if snapshot.operation in {"create", "cancel"}:
            assert snapshot.start_at is not None
            local = snapshot.start_at.astimezone(timezone)
            for payload in (patient, owner):
                payload.update(
                    date=local.strftime("%A, %b %d"),
                    time=local.strftime("%-I:%M %p"),
                )
        if snapshot.operation == "create":
            patient["price"] = f"₹{snapshot.price}" if snapshot.price else None
        elif snapshot.operation == "cancel":
            patient["reason"] = snapshot.reason
            owner["reason"] = snapshot.reason
        else:
            assert snapshot.old_start_at is not None
            assert snapshot.new_start_at is not None
            old_local = snapshot.old_start_at.astimezone(timezone)
            new_local = snapshot.new_start_at.astimezone(timezone)
            for payload in (patient, owner):
                payload.update(
                    old_date=old_local.strftime("%A, %b %d"),
                    old_time=old_local.strftime("%-I:%M %p"),
                    new_date=new_local.strftime("%A, %b %d"),
                    new_time=new_local.strftime("%-I:%M %p"),
                )
        return patient, owner

    def _assert_base_event(
        self,
        event: NotificationOutboxEvent,
        *,
        business_id: int,
        key: str,
        event_type: str,
        appointment_id: int,
        recipient_type: str,
    ) -> None:
        if (
            event.business_id != business_id
            or event.idempotency_key != key
            or event.event_type != event_type
            or event.entity_type != "appointment"
            or event.entity_id != appointment_id
            or event.recipient_type != recipient_type
            or event.channel != NotificationChannel.WHATSAPP.value
            or not event.recipient_phone
            or not self._valid_status(event)
        ):
            raise NotificationIdempotencyConflictError(
                "Committed notification row does not match required identity"
            )

    def _legacy_snapshot(
        self,
        patient: NotificationOutboxEvent,
        owner: NotificationOutboxEvent,
        *,
        operation: Literal["create", "cancel", "reschedule"],
        business_id: int,
        appointment_id: int,
        pending_action_id: int | None,
        patient_phone: str,
        patient_name: str | None,
        service_name: str,
        resource_name: str,
        business_timezone: str,
        start_at: datetime | None,
        old_start_at: datetime | None,
        new_start_at: datetime | None,
        price: object | None,
        reason: str | None,
    ) -> NotificationPairSnapshot:
        patient_payload = patient.payload
        owner_payload = owner.payload
        assert isinstance(patient_payload, dict)
        assert isinstance(owner_payload, dict)
        phone_number_id = patient_payload.get("phone_number_id")
        clinic_name = patient_payload.get("clinic_name")
        if (
            not isinstance(phone_number_id, str)
            or not phone_number_id
            or owner_payload.get("phone_number_id") != phone_number_id
            or not isinstance(clinic_name, str)
            or not clinic_name
            or patient.recipient_phone != patient_phone
            or patient.recipient_name != patient_name
            or owner.recipient_name is not None
            or owner_payload.get("patient_phone") != patient_phone
            or owner_payload.get("patient_name") != patient_name
        ):
            raise NotificationIdempotencyConflictError(
                "Legacy notification pair cannot reconstruct immutable identity"
            )
        try:
            snapshot = NotificationPairSnapshot(
                schema_version=1,
                operation=operation,
                business_id=business_id,
                appointment_id=appointment_id,
                pending_action_id=pending_action_id,
                clinic_name=clinic_name,
                patient_phone=patient_phone,
                patient_name=patient_name,
                owner_phone=owner.recipient_phone,
                phone_number_id=phone_number_id,
                service_name=service_name,
                resource_name=resource_name,
                business_timezone=business_timezone,
                start_at=start_at,
                old_start_at=old_start_at,
                new_start_at=new_start_at,
                price=str(price) if price is not None else None,
                reason=reason,
            )
        except Exception as exc:
            raise NotificationIdempotencyConflictError(
                "Legacy notification evidence contains invalid time or facts"
            ) from exc
        expected_patient, expected_owner = self._legacy_payloads(snapshot)
        if patient_payload != expected_patient or owner_payload != expected_owner:
            raise NotificationIdempotencyConflictError(
                "Legacy notification pair is not fully equivalent"
            )
        return snapshot

    async def _verify_or_repair_committed_pair(
        self,
        *,
        business_id: int,
        keys: tuple[str, str],
        operation: Literal["create", "cancel", "reschedule"],
        appointment_id: int,
        patient_phone: str,
        patient_name: str | None,
        service_name: str,
        resource_name: str,
        business_timezone: str,
        start_at: datetime | None = None,
        old_start_at: datetime | None = None,
        new_start_at: datetime | None = None,
        price: object | None = None,
        reason: str | None = None,
        pending_action_id: int | None = None,
    ) -> list[int]:
        patient = await self._repo.get_event_by_idempotency_key(business_id, keys[0])
        owner = await self._repo.get_event_by_idempotency_key(business_id, keys[1])
        existing = [event for event in (patient, owner) if event is not None]
        if not existing:
            raise NotificationIdempotencyConflictError(
                "Required committed notification evidence is missing"
            )
        formats = {self._event_format(event) for event in existing}
        if len(formats) != 1:
            raise NotificationIdempotencyConflictError(
                "Committed notification pair mixes incompatible versions"
            )
        event_type = {
            "create": NotificationEventType.APPOINTMENT_CONFIRMED.value,
            "cancel": NotificationEventType.APPOINTMENT_CANCELLED.value,
            "reschedule": NotificationEventType.APPOINTMENT_RESCHEDULED.value,
        }[operation]
        for event, key, recipient in (
            (patient, keys[0], NotificationRecipientType.PATIENT.value),
            (owner, keys[1], NotificationRecipientType.OWNER.value),
        ):
            if event is not None:
                self._assert_base_event(
                    event,
                    business_id=business_id,
                    key=key,
                    event_type=event_type,
                    appointment_id=appointment_id,
                    recipient_type=recipient,
                )

        if formats == {"legacy"}:
            if patient is None or owner is None:
                raise NotificationIdempotencyConflictError(
                    "Legacy notification member is missing and cannot be reconstructed"
                )
            self._legacy_snapshot(
                patient,
                owner,
                operation=operation,
                business_id=business_id,
                appointment_id=appointment_id,
                pending_action_id=pending_action_id,
                patient_phone=patient_phone,
                patient_name=patient_name,
                service_name=service_name,
                resource_name=resource_name,
                business_timezone=business_timezone,
                start_at=start_at,
                old_start_at=old_start_at,
                new_start_at=new_start_at,
                price=price,
                reason=reason,
            )
            return [patient.id, owner.id]

        parsed: list[NotificationPairSnapshot] = []
        for event in existing:
            assert isinstance(event.payload, dict)
            try:
                snapshot = NotificationPairSnapshot.model_validate(
                    event.payload["equivalence_snapshot"]
                )
            except Exception as exc:
                raise NotificationIdempotencyConflictError(
                    "Committed notification equivalence snapshot is invalid"
                ) from exc
            if event.payload.get("equivalence_digest") != self._snapshot_digest(snapshot):
                raise NotificationIdempotencyConflictError(
                    "Committed notification equivalence digest is invalid"
                )
            parsed.append(snapshot)
        snapshot = parsed[0]
        if any(item != snapshot for item in parsed[1:]) or self._keys(snapshot) != keys:
            raise NotificationIdempotencyConflictError(
                "Committed notification pair snapshots disagree"
            )
        self._assert_snapshot_facts(
            snapshot,
            operation=operation,
            business_id=business_id,
            appointment_id=appointment_id,
            patient_phone=patient_phone,
            patient_name=patient_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            start_at=start_at,
            old_start_at=old_start_at,
            new_start_at=new_start_at,
            price=price,
            reason=reason,
            pending_action_id=pending_action_id,
        )
        expected_values = self._event_values(snapshot)
        for event, expected in zip((patient, owner), expected_values, strict=True):
            if event is not None and not self._event_equivalent(event, expected):
                raise NotificationIdempotencyConflictError(
                    "Committed notification member is not fully equivalent"
                )

        async with self._session.begin_nested():
            ids = []
            for event, expected in zip((patient, owner), expected_values, strict=True):
                ids.append(
                    event.id if event is not None else await self._insert_or_verify(expected)
                )
        return ids

    @staticmethod
    def _assert_snapshot_facts(
        snapshot: NotificationPairSnapshot,
        *,
        operation: str,
        business_id: int,
        appointment_id: int,
        patient_phone: str,
        patient_name: str | None,
        service_name: str,
        resource_name: str,
        business_timezone: str,
        start_at: datetime | None = None,
        old_start_at: datetime | None = None,
        new_start_at: datetime | None = None,
        price: object | None = None,
        reason: str | None = None,
        pending_action_id: int | None = None,
    ) -> None:
        expected = {
            "operation": operation,
            "business_id": business_id,
            "appointment_id": appointment_id,
            "patient_phone": patient_phone,
            "patient_name": patient_name,
            "service_name": service_name,
            "resource_name": resource_name,
            "business_timezone": business_timezone,
            "pending_action_id": pending_action_id,
            "price": str(price) if price is not None else None,
            "reason": reason,
        }
        actual = {name: getattr(snapshot, name) for name in expected}
        if actual != expected:
            raise NotificationIdempotencyConflictError(
                "Committed notification snapshot conflicts with appointment evidence"
            )
        for actual_time, expected_time in (
            (snapshot.start_at, start_at),
            (snapshot.old_start_at, old_start_at),
            (snapshot.new_start_at, new_start_at),
        ):
            if (actual_time is None) != (expected_time is None) or (
                actual_time is not None
                and expected_time is not None
                and instant(actual_time) != instant(expected_time)
            ):
                raise NotificationIdempotencyConflictError(
                    "Committed notification time conflicts with appointment evidence"
                )

    async def _new_snapshot(
        self,
        *,
        operation: Literal["create", "cancel", "reschedule"],
        business_id: int,
        appointment_id: int,
        pending_action_id: int | None,
        customer_phone: str,
        customer_name: str | None,
        service_name: str,
        resource_name: str,
        business_timezone: str,
        start_at: datetime | None = None,
        old_start_at: datetime | None = None,
        new_start_at: datetime | None = None,
        price: object | None = None,
        reason: str | None = None,
    ) -> NotificationPairSnapshot:
        business, owner_phone = await self._business_and_owner_phone(business_id)
        phone_number_id = WhatsAppBusinessMapping().get_phone_number_id(
            business_id, preferred=settings.whatsapp_phone_number_id or None
        )
        if phone_number_id is None:
            raise RuntimeError("whatsapp_business_mapping_missing_or_ambiguous")
        return NotificationPairSnapshot(
            schema_version=1,
            operation=operation,
            business_id=business_id,
            appointment_id=appointment_id,
            pending_action_id=pending_action_id,
            clinic_name=business.name,
            patient_phone=customer_phone,
            patient_name=customer_name,
            owner_phone=owner_phone,
            phone_number_id=phone_number_id,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            start_at=start_at,
            old_start_at=old_start_at,
            new_start_at=new_start_at,
            price=str(price) if price is not None else None,
            reason=reason,
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
        snapshot = await self._new_snapshot(
            operation="create",
            business_id=business_id,
            appointment_id=appointment_id,
            pending_action_id=None,
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            start_at=start_at,
            price=price,
        )
        return await self._create_or_verify_pair(snapshot)

    async def verify_appointment_notifications(
        self,
        *,
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
        keys = (
            f"appt-confirm-patient-{appointment_id}",
            f"appt-confirm-owner-{appointment_id}",
        )
        return await self._verify_or_repair_committed_pair(
            business_id=business_id,
            keys=keys,
            operation="create",
            appointment_id=appointment_id,
            patient_phone=customer_phone,
            patient_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            start_at=start_at,
            price=price,
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
        snapshot = await self._new_snapshot(
            operation="cancel",
            business_id=business_id,
            appointment_id=appointment_id,
            pending_action_id=None,
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            start_at=start_at,
            reason=reason,
        )
        return await self._create_or_verify_pair(snapshot)

    async def verify_cancellation_notifications(
        self,
        *,
        business_id: int,
        appointment_id: int,
        customer_phone: str,
        customer_name: str | None,
        service_name: str,
        resource_name: str,
        start_at: datetime,
        business_timezone: str,
        reason: str | None,
    ) -> list[int]:
        keys = (
            f"appt-cancel-patient-{appointment_id}",
            f"appt-cancel-owner-{appointment_id}",
        )
        return await self._verify_or_repair_committed_pair(
            business_id=business_id,
            keys=keys,
            operation="cancel",
            appointment_id=appointment_id,
            patient_phone=customer_phone,
            patient_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            start_at=start_at,
            reason=reason,
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
        snapshot = await self._new_snapshot(
            operation="reschedule",
            business_id=business_id,
            appointment_id=appointment_id,
            pending_action_id=pending_action_id,
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            old_start_at=old_start_at,
            new_start_at=new_start_at,
        )
        return await self._create_or_verify_pair(snapshot)

    async def verify_reschedule_notifications(
        self,
        *,
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
        keys = (
            f"appt-resched-patient-{appointment_id}-{pending_action_id}",
            f"appt-resched-owner-{appointment_id}-{pending_action_id}",
        )
        return await self._verify_or_repair_committed_pair(
            business_id=business_id,
            keys=keys,
            operation="reschedule",
            appointment_id=appointment_id,
            patient_phone=customer_phone,
            patient_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            pending_action_id=pending_action_id,
            old_start_at=old_start_at,
            new_start_at=new_start_at,
        )
