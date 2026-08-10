"""Notification outbox service — creates events inside the caller's transaction.

Exact-v1 evidence policy: only versioned v1+ payloads with equivalence_snapshot
and equivalence_digest authorize automated replay or repair. All non-exact
historical formats produce legacy_unverifiable and require manual reconciliation.
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.config import settings
from fonely.core.metrics import metrics
from fonely.models.enums import (
    NotificationChannel,
    NotificationEventType,
    NotificationRecipientType,
    NotificationStatus,
)
from fonely.models.schema import Business, BusinessUser, NotificationOutboxEvent
from fonely.repositories.notifications import NotificationRepository
from fonely.services.whatsapp_config import WhatsAppBusinessMapping

logger = logging.getLogger("fonely.services.notifications")

_METRIC = "notification_reconciliation_total"


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

    # ── digest helpers ──────────────────────────────────────────────

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

    # ── idempotency keys ────────────────────────────────────────────

    @staticmethod
    def _keys(snapshot: NotificationPairSnapshot) -> tuple[str, str]:
        op = snapshot.operation
        aid = snapshot.appointment_id
        if op == "create":
            return (
                f"appt-confirm-patient-{aid}",
                f"appt-confirm-owner-{aid}",
            )
        if op == "cancel":
            return (
                f"appt-cancel-patient-{aid}",
                f"appt-cancel-owner-{aid}",
            )
        pa = snapshot.pending_action_id
        return (
            f"appt-resched-patient-{aid}-{pa}",
            f"appt-resched-owner-{aid}-{pa}",
        )

    # ── event value builders ────────────────────────────────────────

    def _event_values(
        self, snapshot: NotificationPairSnapshot
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        tz = ZoneInfo(snapshot.business_timezone)
        equivalence = snapshot.model_dump(mode="json")
        digest = self._snapshot_digest(snapshot)
        keys = self._keys(snapshot)

        event_type_map = {
            "create": NotificationEventType.APPOINTMENT_CONFIRMED.value,
            "cancel": NotificationEventType.APPOINTMENT_CANCELLED.value,
            "reschedule": NotificationEventType.APPOINTMENT_RESCHEDULED.value,
        }
        event_type = event_type_map[snapshot.operation]
        template_map = {
            "create": "appointment_confirmed",
            "cancel": "appointment_cancelled",
            "reschedule": "appointment_rescheduled",
        }
        template_type = template_map[snapshot.operation]

        patient_payload: dict[str, Any] = {
            "schema_version": 1,
            "template_type": template_type,
            "clinic_name": snapshot.clinic_name,
            "service": snapshot.service_name,
            "doctor": snapshot.resource_name,
            "appointment_id": snapshot.appointment_id,
            "phone_number_id": snapshot.phone_number_id,
            "equivalence_snapshot": equivalence,
            "equivalence_digest": digest,
        }
        owner_payload: dict[str, Any] = {
            "schema_version": 1,
            "template_type": template_type,
            "patient_name": snapshot.patient_name,
            "patient_phone": snapshot.patient_phone,
            "service": snapshot.service_name,
            "doctor": snapshot.resource_name,
            "appointment_id": snapshot.appointment_id,
            "phone_number_id": snapshot.phone_number_id,
            "equivalence_snapshot": equivalence,
            "equivalence_digest": digest,
        }

        if snapshot.operation in {"create", "cancel"}:
            assert snapshot.start_at is not None
            local = snapshot.start_at.astimezone(tz)
            for payload in (patient_payload, owner_payload):
                payload["date"] = local.strftime("%A, %b %d")
                payload["time"] = local.strftime("%-I:%M %p")

        if snapshot.operation == "create":
            patient_payload["price"] = f"₹{snapshot.price}" if snapshot.price else None
        elif snapshot.operation == "cancel":
            patient_payload["reason"] = snapshot.reason
            owner_payload["reason"] = snapshot.reason
        else:
            assert snapshot.old_start_at is not None
            assert snapshot.new_start_at is not None
            old_local = snapshot.old_start_at.astimezone(tz)
            new_local = snapshot.new_start_at.astimezone(tz)
            for payload in (patient_payload, owner_payload):
                payload["old_date"] = old_local.strftime("%A, %b %d")
                payload["old_time"] = old_local.strftime("%-I:%M %p")
                payload["new_date"] = new_local.strftime("%A, %b %d")
                payload["new_time"] = new_local.strftime("%-I:%M %p")

        base = {
            "business_id": snapshot.business_id,
            "event_type": event_type,
            "entity_type": "appointment",
            "entity_id": snapshot.appointment_id,
            "channel": NotificationChannel.WHATSAPP.value,
            "status": NotificationStatus.PENDING.value,
        }

        patient = {
            **base,
            "recipient_type": NotificationRecipientType.PATIENT.value,
            "recipient_phone": snapshot.patient_phone,
            "recipient_name": snapshot.patient_name,
            "payload": patient_payload,
            "idempotency_key": keys[0],
        }
        owner = {
            **base,
            "recipient_type": NotificationRecipientType.OWNER.value,
            "recipient_phone": snapshot.owner_phone,
            "recipient_name": None,
            "payload": owner_payload,
            "idempotency_key": keys[1],
        }
        return patient, owner

    # ── equivalence comparison ──────────────────────────────────────

    @staticmethod
    def _event_equivalent(persisted: NotificationOutboxEvent, expected: dict[str, Any]) -> bool:
        for field in (
            "business_id",
            "event_type",
            "entity_type",
            "entity_id",
            "recipient_type",
            "recipient_phone",
            "recipient_name",
            "channel",
            "idempotency_key",
        ):
            if getattr(persisted, field) != expected[field]:
                return False
        if persisted.status not in {
            NotificationStatus.PENDING.value,
            NotificationStatus.PROCESSING.value,
            NotificationStatus.DELIVERED.value,
            NotificationStatus.FAILED.value,
            NotificationStatus.DEAD_LETTER.value,
            NotificationStatus.UNKNOWN.value,
        }:
            return False
        persisted_payload = persisted.payload
        expected_payload = expected["payload"]
        if not isinstance(persisted_payload, dict) or not isinstance(expected_payload, dict):
            return False
        if set(persisted_payload.keys()) != set(expected_payload.keys()):
            return False
        return all(persisted_payload.get(key) == expected_payload[key] for key in expected_payload)

    # ── format detection ────────────────────────────────────────────

    @staticmethod
    def _event_format(
        event: NotificationOutboxEvent,
    ) -> Literal["v1", "legacy"]:
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

    # ── owner resolution ────────────────────────────────────────────

    async def _business_and_owner_phone(self, business_id: int) -> tuple[str, str]:
        business = await self._session.scalar(select(Business).where(Business.id == business_id))
        if business is None:
            raise RuntimeError("business_not_found")
        clinic_name = business.name or "Business"
        owners = (
            await self._session.scalars(
                select(BusinessUser).where(
                    BusinessUser.business_id == business_id,
                    BusinessUser.role == "owner",
                    BusinessUser.is_active.is_(True),
                )
            )
        ).all()
        if len(owners) == 0:
            raise RuntimeError("active_owner_not_found")
        if len(owners) > 1:
            raise RuntimeError("multiple_active_owners")
        return clinic_name, owners[0].phone

    # ── insert or verify single event ───────────────────────────────

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
                "Existing notification event is not equivalent to expected values"
            )
        return existing.id

    # ── snapshot fact assertion ──────────────────────────────────────

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
        price_str = str(price) if price is not None else None
        checks = [
            snapshot.operation == operation,
            snapshot.business_id == business_id,
            snapshot.appointment_id == appointment_id,
            snapshot.patient_phone == patient_phone,
            snapshot.patient_name == patient_name,
            snapshot.service_name == service_name,
            snapshot.resource_name == resource_name,
            snapshot.business_timezone == business_timezone,
            snapshot.price == price_str,
            snapshot.reason == reason,
            snapshot.pending_action_id == pending_action_id,
        ]
        if start_at is not None:
            checks.append(snapshot.start_at == start_at)
        if old_start_at is not None:
            checks.append(snapshot.old_start_at == old_start_at)
        if new_start_at is not None:
            checks.append(snapshot.new_start_at == new_start_at)
        if not all(checks):
            raise NotificationIdempotencyConflictError(
                "Committed notification snapshot does not match immutable facts"
            )

    # ── metric helper ───────────────────────────────────────────────

    @staticmethod
    def _emit_metric(
        operation: str,
        fmt: str,
        outcome: str,
    ) -> None:
        metrics.increment(
            _METRIC,
            {"operation": operation, "format": fmt, "outcome": outcome},
        )

    # ── create or verify pair (fresh insert) ────────────────────────

    async def _create_or_verify_pair(self, snapshot: NotificationPairSnapshot) -> list[int]:
        patient_values, owner_values = self._event_values(snapshot)
        async with self._session.begin_nested():
            patient_id = await self._insert_or_verify(patient_values)
            owner_id = await self._insert_or_verify(owner_values)
        return [patient_id, owner_id]

    # ── verify or repair committed pair (replay) ────────────────────

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

        existing = [e for e in (patient, owner) if e is not None]
        if not existing:
            self._emit_metric(operation, "none", "missing_evidence")
            raise NotificationIdempotencyConflictError(
                "Required committed notification evidence is missing"
            )

        # Format detection — legacy fails closed immediately
        try:
            formats = {self._event_format(e) for e in existing}
        except NotificationIdempotencyConflictError:
            self._emit_metric(operation, "partial", "legacy_unverifiable")
            raise

        if "legacy" in formats:
            fmt = "mixed" if "v1" in formats else "legacy"
            self._emit_metric(operation, fmt, "legacy_unverifiable")
            raise NotificationIdempotencyConflictError(
                "Non-exact legacy notification evidence requires manual "
                "reconciliation (legacy_unverifiable)"
            )

        if len(formats) != 1 or formats != {"v1"}:
            self._emit_metric(operation, "mixed", "legacy_unverifiable")
            raise NotificationIdempotencyConflictError(
                "Committed notification pair has incompatible format"
            )

        # Base identity validation
        event_type = {
            "create": NotificationEventType.APPOINTMENT_CONFIRMED.value,
            "cancel": NotificationEventType.APPOINTMENT_CANCELLED.value,
            "reschedule": NotificationEventType.APPOINTMENT_RESCHEDULED.value,
        }[operation]
        for event, key, recipient in (
            (patient, keys[0], NotificationRecipientType.PATIENT.value),
            (owner, keys[1], NotificationRecipientType.OWNER.value),
        ):
            if event is not None and (
                event.business_id != business_id
                or event.idempotency_key != key
                or event.event_type != event_type
                or event.entity_type != "appointment"
                or event.entity_id != appointment_id
                or event.recipient_type != recipient
                or event.channel != NotificationChannel.WHATSAPP.value
                or not event.recipient_phone
            ):
                self._emit_metric(operation, "v1", "evidence_conflict")
                raise NotificationIdempotencyConflictError(
                    "Committed notification row does not match required identity"
                )

        # Parse and verify v1 snapshots
        parsed: list[NotificationPairSnapshot] = []
        for event in existing:
            assert isinstance(event.payload, dict)
            try:
                snapshot = NotificationPairSnapshot.model_validate(
                    event.payload["equivalence_snapshot"]
                )
            except Exception as exc:
                self._emit_metric(operation, "v1", "evidence_conflict")
                raise NotificationIdempotencyConflictError(
                    "Committed notification equivalence snapshot is invalid"
                ) from exc
            if event.payload.get("equivalence_digest") != self._snapshot_digest(snapshot):
                self._emit_metric(operation, "v1", "evidence_conflict")
                raise NotificationIdempotencyConflictError(
                    "Committed notification equivalence digest is invalid"
                )
            parsed.append(snapshot)

        snapshot = parsed[0]
        if any(item != snapshot for item in parsed[1:]) or self._keys(snapshot) != keys:
            self._emit_metric(operation, "v1", "evidence_conflict")
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
                self._emit_metric(operation, "v1", "evidence_conflict")
                raise NotificationIdempotencyConflictError(
                    "Committed notification member is not fully equivalent"
                )

        # Both present and exact
        if patient is not None and owner is not None:
            self._emit_metric(operation, "v1", "exact_existing")
            return [patient.id, owner.id]

        # One missing — repair inside savepoint with locked revalidation
        async with self._session.begin_nested():
            locked_patient = await self._repo.get_event_by_idempotency_key(
                business_id, keys[0], lock=True
            )
            locked_owner = await self._repo.get_event_by_idempotency_key(
                business_id, keys[1], lock=True
            )
            for locked, expected in zip(
                (locked_patient, locked_owner), expected_values, strict=True
            ):
                if locked is not None and not self._event_equivalent(locked, expected):
                    self._emit_metric(operation, "v1", "evidence_conflict")
                    raise NotificationIdempotencyConflictError(
                        "Committed notification member changed during repair"
                    )
            if locked_patient is not None and locked_owner is not None:
                self._emit_metric(operation, "v1", "exact_existing")
                return [locked_patient.id, locked_owner.id]
            ids = []
            for locked, expected in zip(
                (locked_patient, locked_owner), expected_values, strict=True
            ):
                if locked is not None:
                    ids.append(locked.id)
                else:
                    ids.append(await self._insert_or_verify(expected))
        self._emit_metric(operation, "v1", "exact_repaired")
        return ids

    # ── build snapshot from business facts ──────────────────────────

    async def _build_snapshot(
        self,
        *,
        operation: Literal["create", "cancel", "reschedule"],
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
    ) -> NotificationPairSnapshot:
        clinic_name, owner_phone = await self._business_and_owner_phone(business_id)
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
            clinic_name=clinic_name,
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

    # ── public create methods ───────────────────────────────────────

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
        snapshot = await self._build_snapshot(
            operation="create",
            business_id=business_id,
            appointment_id=appointment_id,
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            start_at=start_at,
            price=price,
        )
        return await self._create_or_verify_pair(snapshot)

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
        snapshot = await self._build_snapshot(
            operation="cancel",
            business_id=business_id,
            appointment_id=appointment_id,
            customer_phone=customer_phone,
            customer_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            start_at=start_at,
            reason=reason,
        )
        return await self._create_or_verify_pair(snapshot)

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
        snapshot = await self._build_snapshot(
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

    # ── public verify methods (replay, no current config needed) ────

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
        op: Literal["create"] = "create"
        keys = (
            f"appt-confirm-patient-{appointment_id}",
            f"appt-confirm-owner-{appointment_id}",
        )
        return await self._verify_or_repair_committed_pair(
            business_id=business_id,
            keys=keys,
            operation=op,
            appointment_id=appointment_id,
            patient_phone=customer_phone,
            patient_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            start_at=start_at,
            price=price,
        )

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
        reason: str | None = None,
    ) -> list[int]:
        op: Literal["cancel"] = "cancel"
        keys = (
            f"appt-cancel-patient-{appointment_id}",
            f"appt-cancel-owner-{appointment_id}",
        )
        return await self._verify_or_repair_committed_pair(
            business_id=business_id,
            keys=keys,
            operation=op,
            appointment_id=appointment_id,
            patient_phone=customer_phone,
            patient_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            start_at=start_at,
            reason=reason,
        )

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
        op: Literal["reschedule"] = "reschedule"
        keys = (
            f"appt-resched-patient-{appointment_id}-{pending_action_id}",
            f"appt-resched-owner-{appointment_id}-{pending_action_id}",
        )
        return await self._verify_or_repair_committed_pair(
            business_id=business_id,
            keys=keys,
            operation=op,
            appointment_id=appointment_id,
            patient_phone=customer_phone,
            patient_name=customer_name,
            service_name=service_name,
            resource_name=resource_name,
            business_timezone=business_timezone,
            old_start_at=old_start_at,
            new_start_at=new_start_at,
            pending_action_id=pending_action_id,
        )
