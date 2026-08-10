"""Notification outbox service — durable, idempotent, v1-snapshot-verified.

Every public method either creates a notification pair (patient + owner)
inside the caller's transaction, or verifies a previously committed pair
against the current business facts.  Both paths use the same canonical
snapshot model so drift is detected deterministically.
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

# ---------------------------------------------------------------------------
# Snapshot model
# ---------------------------------------------------------------------------


class NotificationPairSnapshot(BaseModel):
    """Canonical representation of every fact a notification pair depends on.

    Stored as ``equivalence_snapshot`` inside each outbox event payload so
    later verification can compare the committed state against fresh
    business facts without re-deriving presentation strings.
    """

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


# ---------------------------------------------------------------------------
# Custom error
# ---------------------------------------------------------------------------


class NotificationIdempotencyConflictError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Digest helpers
# ---------------------------------------------------------------------------


def _canonical_digest(data: dict[str, Any]) -> str:
    """SHA-256 hex of the JSON-serialised *data* with sorted keys."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _snapshot_digest(snapshot: NotificationPairSnapshot) -> str:
    """Digest the Pydantic snapshot via its dict representation."""
    return _canonical_digest(snapshot.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_EVENT_TYPE_MAP: dict[str, str] = {
    "create": NotificationEventType.APPOINTMENT_CONFIRMED.value,
    "cancel": NotificationEventType.APPOINTMENT_CANCELLED.value,
    "reschedule": NotificationEventType.APPOINTMENT_RESCHEDULED.value,
}


def _keys(
    operation: str, appointment_id: int, pending_action_id: int | None = None
) -> tuple[str, str]:
    """Return (patient_key, owner_key) idempotency strings."""
    if operation == "reschedule":
        suffix = f"{appointment_id}-pa{pending_action_id}"
    else:
        suffix = str(appointment_id)
    prefix_map = {
        "create": "appt-confirm",
        "cancel": "appt-cancel",
        "reschedule": "appt-reschedule",
    }
    prefix = prefix_map[operation]
    return (f"{prefix}-patient-{suffix}", f"{prefix}-owner-{suffix}")


def _event_values(
    snapshot: NotificationPairSnapshot,
    recipient_type: str,
    recipient_phone: str,
    recipient_name: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Build the column-value dict for a single outbox event insert."""
    local_time = (
        snapshot.start_at.astimezone(ZoneInfo(snapshot.business_timezone))
        if snapshot.start_at
        else None
    )

    template_map = {
        "create": "appointment_confirmed",
        "cancel": "appointment_cancelled",
        "reschedule": "appointment_rescheduled",
    }

    # Base payload shared by all recipient types
    payload: dict[str, Any] = {
        "schema_version": 1,
        "template_type": template_map[snapshot.operation],
        "appointment_id": snapshot.appointment_id,
        "phone_number_id": snapshot.phone_number_id,
        "service": snapshot.service_name,
        "doctor": snapshot.resource_name,
    }

    if snapshot.operation == "reschedule":
        old_local = (
            snapshot.old_start_at.astimezone(ZoneInfo(snapshot.business_timezone))
            if snapshot.old_start_at
            else None
        )
        new_local = (
            snapshot.new_start_at.astimezone(ZoneInfo(snapshot.business_timezone))
            if snapshot.new_start_at
            else None
        )
        if old_local:
            payload["old_date"] = old_local.strftime("%A, %b %d")
            payload["old_time"] = old_local.strftime("%-I:%M %p")
        if new_local:
            payload["new_date"] = new_local.strftime("%A, %b %d")
            payload["new_time"] = new_local.strftime("%-I:%M %p")
    else:
        if local_time:
            payload["date"] = local_time.strftime("%A, %b %d")
            payload["time"] = local_time.strftime("%-I:%M %p")

    if recipient_type == NotificationRecipientType.PATIENT.value:
        payload["clinic_name"] = snapshot.clinic_name
        if snapshot.operation == "create" and snapshot.price is not None:
            payload["price"] = f"₹{snapshot.price}"
        if snapshot.operation == "cancel" and snapshot.reason is not None:
            payload["reason"] = snapshot.reason
    else:
        # Owner gets patient identity
        payload["patient_name"] = snapshot.patient_name
        payload["patient_phone"] = snapshot.patient_phone
        if snapshot.operation == "cancel" and snapshot.reason is not None:
            payload["reason"] = snapshot.reason

    # Attach v1 equivalence proof
    digest = _snapshot_digest(snapshot)
    payload["equivalence_snapshot"] = snapshot.model_dump(mode="json")
    payload["equivalence_digest"] = digest

    event_type = _EVENT_TYPE_MAP[snapshot.operation]

    return {
        "business_id": snapshot.business_id,
        "event_type": event_type,
        "entity_type": "appointment",
        "entity_id": snapshot.appointment_id,
        "recipient_type": recipient_type,
        "recipient_phone": recipient_phone,
        "recipient_name": recipient_name,
        "channel": NotificationChannel.WHATSAPP.value,
        "payload": payload,
        "status": NotificationStatus.PENDING.value,
        "idempotency_key": idempotency_key,
    }


# ---------------------------------------------------------------------------
# Equivalence comparison
# ---------------------------------------------------------------------------


def _event_equivalent(
    persisted: NotificationOutboxEvent,
    expected_values: dict[str, Any],
) -> bool:
    """Return True when every material field of *persisted* matches *expected_values*.

    Payload comparison is key-set EQUALITY (not subset) so that extra or
    missing keys are caught.
    """
    for col in (
        "business_id",
        "event_type",
        "entity_type",
        "entity_id",
        "recipient_type",
        "recipient_phone",
        "channel",
    ):
        if getattr(persisted, col) != expected_values[col]:
            return False

    persisted_payload: dict[str, Any] = persisted.payload or {}
    expected_payload: dict[str, Any] = expected_values.get("payload", {})

    if set(persisted_payload.keys()) != set(expected_payload.keys()):
        return False

    return all(persisted_payload.get(key) == expected_payload[key] for key in expected_payload)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def _event_format(event: NotificationOutboxEvent) -> Literal["v1", "legacy"]:
    """Detect whether an outbox event carries a v1 snapshot or is legacy."""
    if not isinstance(event.payload, dict):
        raise NotificationIdempotencyConflictError("Committed notification payload is malformed")
    has_snapshot = "equivalence_snapshot" in event.payload
    has_digest = "equivalence_digest" in event.payload
    if has_snapshot != has_digest:
        raise NotificationIdempotencyConflictError(
            "Committed notification format is partially versioned"
        )
    return "v1" if has_snapshot else "legacy"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = NotificationRepository(session)

    # -- owner resolution ---------------------------------------------------

    async def _business_and_owner_phone(self, business_id: int) -> tuple[Business, str]:
        """Return the Business row and the active owner's phone.

        Queries ``BusinessUser`` (not ``Business.primary_contact_phone``)
        because the users table is the sole authority for owner identity.
        Raises if zero or multiple active owners are found.
        """
        business = await self._session.scalar(select(Business).where(Business.id == business_id))
        if business is None:
            raise RuntimeError(f"business_not_found: {business_id}")

        owners = (
            await self._session.scalars(
                select(BusinessUser).where(
                    BusinessUser.business_id == business_id,
                    BusinessUser.role == "owner",
                    BusinessUser.is_active.is_(True),
                )
            )
        ).all()

        if len(owners) != 1:
            raise RuntimeError(
                f"expected_exactly_one_active_owner: business_id={business_id} found={len(owners)}"
            )

        return business, owners[0].phone

    # -- single-event insert or verify --------------------------------------

    async def _insert_or_verify(
        self,
        values: dict[str, Any],
    ) -> tuple[NotificationOutboxEvent, bool]:
        """Insert a new outbox event or verify idempotent equivalence.

        Uses the repository's ``insert_event_idempotent`` (ON CONFLICT DO
        NOTHING).  When the insert returns None the event already exists;
        we load it and check equivalence.
        """
        event = await self._repo.insert_event_idempotent(values)
        if event is not None:
            return event, True

        # Already existed — load and verify
        existing = await self._repo.get_event_by_idempotency_key(
            values["business_id"], values["idempotency_key"]
        )
        if existing is None:
            raise RuntimeError(
                "notification_idempotency_ghost: insert returned None but lookup found nothing"
            )

        if not _event_equivalent(existing, values):
            raise NotificationIdempotencyConflictError(
                "Existing notification event is not equivalent to expected values"
            )

        return existing, False

    # -- snapshot fact assertion ---------------------------------------------

    @staticmethod
    def _assert_snapshot_facts(
        committed: NotificationPairSnapshot,
        fresh: NotificationPairSnapshot,
    ) -> None:
        """Raise if any fact in *committed* differs from *fresh*.

        Compares every field in the snapshot so that drift in any
        business fact (clinic name, timezone, owner phone, etc.) is
        caught deterministically.
        """
        committed_dict = committed.model_dump(mode="json")
        fresh_dict = fresh.model_dump(mode="json")

        mismatches: list[str] = []
        for key in committed_dict:
            if committed_dict[key] != fresh_dict[key]:
                mismatches.append(
                    f"{key}: committed={committed_dict[key]!r} fresh={fresh_dict[key]!r}"
                )

        if mismatches:
            raise RuntimeError("notification_snapshot_drift: " + "; ".join(mismatches))

    # -- metrics ------------------------------------------------------------

    @staticmethod
    def _emit_metric(outcome: str, operation: str) -> None:
        metrics.increment(
            "notification_reconciliation_total",
            {"outcome": outcome, "operation": operation},
        )

    # -- pair creation (fresh insert in savepoint) --------------------------

    async def _create_or_verify_pair(
        self,
        snapshot: NotificationPairSnapshot,
    ) -> list[int]:
        """Insert (or verify) a patient + owner notification pair.

        Each insert runs inside its own savepoint so a partial failure
        does not corrupt the outer transaction.
        """
        patient_key, owner_key = _keys(
            snapshot.operation,
            snapshot.appointment_id,
            snapshot.pending_action_id,
        )

        patient_values = _event_values(
            snapshot,
            recipient_type=NotificationRecipientType.PATIENT.value,
            recipient_phone=snapshot.patient_phone,
            recipient_name=snapshot.patient_name,
            idempotency_key=patient_key,
        )
        owner_values = _event_values(
            snapshot,
            recipient_type=NotificationRecipientType.OWNER.value,
            recipient_phone=snapshot.owner_phone,
            recipient_name=None,
            idempotency_key=owner_key,
        )

        event_ids: list[int] = []

        async with self._session.begin_nested():
            patient_event, patient_inserted = await self._insert_or_verify(patient_values)
            if patient_inserted:
                event_ids.append(patient_event.id)

        async with self._session.begin_nested():
            owner_event, owner_inserted = await self._insert_or_verify(owner_values)
            if owner_inserted:
                event_ids.append(owner_event.id)

        if patient_inserted or owner_inserted:
            self._emit_metric("fresh_insert", snapshot.operation)
        return event_ids

    # -- pair verification (locked reread + validate) -----------------------

    async def _verify_or_repair_committed_pair(
        self,
        snapshot: NotificationPairSnapshot,
    ) -> list[int]:
        """Verify a previously committed notification pair.

        Steps:
        1. Look up patient/owner by idempotency keys.
        2. Fail if none found (missing_evidence).
        3. Check format; legacy fails closed immediately.
        4. Validate base identity fields.
        5. Parse and verify v1 snapshots from equivalence_snapshot.
        6. Assert snapshot facts match.
        7. If both present and equivalent -> exact_existing.
        8. If one missing -> repair in savepoint with locked reread.
        """
        patient_key, owner_key = _keys(
            snapshot.operation,
            snapshot.appointment_id,
            snapshot.pending_action_id,
        )

        patient_event = await self._repo.get_event_by_idempotency_key(
            snapshot.business_id, patient_key
        )
        owner_event = await self._repo.get_event_by_idempotency_key(snapshot.business_id, owner_key)

        # Step 2 — fail if none found
        if patient_event is None and owner_event is None:
            self._emit_metric("missing_evidence", snapshot.operation)
            raise RuntimeError(
                f"notification_missing_evidence: patient_key={patient_key} owner_key={owner_key}"
            )

        event_ids: list[int] = []

        # Process each event
        for role, event, idem_key in [
            ("patient", patient_event, patient_key),
            ("owner", owner_event, owner_key),
        ]:
            if event is None:
                # Step 8 — repair in savepoint with locked reread
                logger.warning("notification_repair_missing_%s: key=%s", role, idem_key)
                if role == "patient":
                    values = _event_values(
                        snapshot,
                        recipient_type=NotificationRecipientType.PATIENT.value,
                        recipient_phone=snapshot.patient_phone,
                        recipient_name=snapshot.patient_name,
                        idempotency_key=idem_key,
                    )
                else:
                    values = _event_values(
                        snapshot,
                        recipient_type=NotificationRecipientType.OWNER.value,
                        recipient_phone=snapshot.owner_phone,
                        recipient_name=None,
                        idempotency_key=idem_key,
                    )

                async with self._session.begin_nested():
                    repaired, _ = await self._insert_or_verify(values)
                    event_ids.append(repaired.id)

                self._emit_metric("repaired_missing", snapshot.operation)
                continue

            # Step 3 — check format
            fmt = _event_format(event)
            if fmt == "legacy":
                self._emit_metric("legacy_fail_closed", snapshot.operation)
                raise RuntimeError(
                    f"notification_legacy_format_unverifiable: event_id={event.id} key={idem_key}"
                )

            # Step 4 — validate base identity
            if event.business_id != snapshot.business_id:
                raise RuntimeError(
                    f"notification_business_mismatch: "
                    f"event_id={event.id} expected={snapshot.business_id} "
                    f"got={event.business_id}"
                )
            if event.entity_id != snapshot.appointment_id:
                raise RuntimeError(
                    f"notification_entity_mismatch: "
                    f"event_id={event.id} expected={snapshot.appointment_id} "
                    f"got={event.entity_id}"
                )

            # Step 5 — parse v1 snapshot
            payload = event.payload or {}
            committed_snapshot_data = payload.get("equivalence_snapshot")
            if committed_snapshot_data is None:
                raise RuntimeError(f"notification_v1_missing_snapshot: event_id={event.id}")

            committed_snapshot = NotificationPairSnapshot(**committed_snapshot_data)

            # Step 6 — assert facts
            self._assert_snapshot_facts(committed_snapshot, snapshot)

            # Step 7 — equivalent
            event_ids.append(event.id)

        self._emit_metric("exact_existing", snapshot.operation)
        return event_ids

    # -- snapshot builder ---------------------------------------------------

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
        pending_action_id: int | None = None,
        price: Any | None = None,
        reason: str | None = None,
    ) -> NotificationPairSnapshot:
        """Build a snapshot from business facts, resolving owner and
        WhatsApp phone from the database and configuration."""
        business, owner_phone = await self._business_and_owner_phone(business_id)

        phone_number_id = WhatsAppBusinessMapping().get_phone_number_id(
            business_id, preferred=settings.whatsapp_phone_number_id or None
        )
        if phone_number_id is None:
            raise RuntimeError("whatsapp_business_mapping_missing_or_ambiguous")

        from decimal import Decimal as _Decimal

        price_str = str(_Decimal(str(price)).normalize()) if price is not None else None

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
            price=price_str,
            reason=reason,
        )

    # ===================================================================
    # Public API — create
    # ===================================================================

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
            start_at=start_at,
            price=price,
            business_timezone=business_timezone,
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
            start_at=start_at,
            reason=reason,
            business_timezone=business_timezone,
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
            old_start_at=old_start_at,
            new_start_at=new_start_at,
            business_timezone=business_timezone,
        )
        return await self._create_or_verify_pair(snapshot)

    # ===================================================================
    # Public API — verify
    # ===================================================================

    async def verify_appointment_notifications(
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
            start_at=start_at,
            price=price,
            business_timezone=business_timezone,
        )
        return await self._verify_or_repair_committed_pair(snapshot)

    async def verify_cancellation_notifications(
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
            start_at=start_at,
            reason=reason,
            business_timezone=business_timezone,
        )
        return await self._verify_or_repair_committed_pair(snapshot)

    async def verify_reschedule_notifications(
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
            old_start_at=old_start_at,
            new_start_at=new_start_at,
            business_timezone=business_timezone,
        )
        return await self._verify_or_repair_committed_pair(snapshot)
