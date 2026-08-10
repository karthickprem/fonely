"""Persisted, short-lived references to slots offered during a conversation."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from fonely.core.validators import AwareDatetime, IANATimezone, PositiveIntegerId
from fonely.domain.appointments.datetimes import instant
from fonely.services.availability import AvailableSlot

_OFFER_SCHEMA_VERSION = 1
_MAX_OFFER_SLOTS = 20
_MAX_SERIALIZED_OFFER_BYTES = 32_768
_DEFAULT_OFFER_TTL = timedelta(minutes=5)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_ORDINALS = {
    "first": 1,
    "the first one": 1,
    "second": 2,
    "the second one": 2,
    "third": 3,
    "the third one": 3,
}
_TIME_PATTERN = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)


class OfferSelectionStatus(StrEnum):
    SELECTED = "selected"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"
    EXPIRED = "expired"
    INVALID = "invalid"


class SelectedSlotRef(BaseModel):
    """One server-issued slot reference stored in authoritative conversation state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    slot_token: str = Field(min_length=20, max_length=64)
    offer_id: str = Field(min_length=20, max_length=64)
    business_id: PositiveIntegerId
    conversation_id: str = Field(min_length=1, max_length=100)
    service_id: PositiveIntegerId
    resource_id: PositiveIntegerId
    resource_name: str = Field(min_length=1, max_length=200)
    start_at: AwareDatetime
    end_at: AwareDatetime
    business_timezone: IANATimezone
    ordinal: int = Field(ge=1, le=_MAX_OFFER_SLOTS)
    expires_at: AwareDatetime
    availability_revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("slot_token", "offer_id")
    @classmethod
    def validate_token_shape(cls, value: str) -> str:
        if not _TOKEN_PATTERN.fullmatch(value):
            raise ValueError("Offer token has invalid shape")
        return value

    @model_validator(mode="after")
    def validate_times(self) -> SelectedSlotRef:
        if instant(self.end_at) <= instant(self.start_at):
            raise ValueError("Offered slot must end after it starts")
        return self

    def display_time(self) -> str:
        return self.start_at.astimezone(ZoneInfo(self.business_timezone)).strftime("%-I:%M %p")


class AvailabilityOffer(BaseModel):
    """The complete active set of alternatives presented to a caller."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    offer_id: str = Field(min_length=20, max_length=64)
    business_id: PositiveIntegerId
    conversation_id: str = Field(min_length=1, max_length=100)
    service_id: PositiveIntegerId
    target_date: date
    business_timezone: IANATimezone
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    availability_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    slots: tuple[SelectedSlotRef, ...] = Field(min_length=1, max_length=_MAX_OFFER_SLOTS)

    @field_validator("offer_id")
    @classmethod
    def validate_offer_id(cls, value: str) -> str:
        if not _TOKEN_PATTERN.fullmatch(value):
            raise ValueError("Offer id has invalid shape")
        return value

    @model_validator(mode="after")
    def validate_offer(self) -> AvailabilityOffer:
        if instant(self.expires_at) <= instant(self.issued_at):
            raise ValueError("Offer must expire after it is issued")
        expected_ordinals = tuple(range(1, len(self.slots) + 1))
        if tuple(slot.ordinal for slot in self.slots) != expected_ordinals:
            raise ValueError("Offer slot ordinals must be contiguous")
        tokens = {slot.slot_token for slot in self.slots}
        if len(tokens) != len(self.slots):
            raise ValueError("Offer slot tokens must be unique")
        for slot in self.slots:
            if (
                slot.offer_id != self.offer_id
                or slot.business_id != self.business_id
                or slot.conversation_id != self.conversation_id
                or slot.service_id != self.service_id
                or slot.business_timezone != self.business_timezone
                or slot.availability_revision != self.availability_revision
                or instant(slot.expires_at) != instant(self.expires_at)
                or slot.start_at.astimezone(ZoneInfo(self.business_timezone)).date()
                != self.target_date
            ):
                raise ValueError("Offer slot does not belong to this offer")
        if self.availability_revision != availability_revision_for_slots(
            business_id=self.business_id,
            conversation_id=self.conversation_id,
            service_id=self.service_id,
            target_date=self.target_date,
            business_timezone=self.business_timezone,
            slots=self.slots,
        ):
            raise ValueError("Offer availability revision does not match its slots")
        return self

    def serialize(self) -> dict[str, Any]:
        value = self.model_dump(mode="json")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_SERIALIZED_OFFER_BYTES:
            raise ValueError("Availability offer exceeds persistence limit")
        return value

    @classmethod
    def deserialize(cls, value: object) -> AvailabilityOffer:
        if not isinstance(value, dict):
            raise ValueError("Availability offer must be an object")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_SERIALIZED_OFFER_BYTES:
            raise ValueError("Availability offer exceeds persistence limit")
        try:
            return cls.model_validate_json(encoded)
        except ValidationError as exc:
            raise ValueError("Availability offer is invalid") from exc


class AvailabilitySelectionState(BaseModel):
    """Atomically persisted active offer and optional selected reference."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    offer: AvailabilityOffer
    selected_slot: SelectedSlotRef | None = None

    @model_validator(mode="after")
    def validate_selected_membership(self) -> AvailabilitySelectionState:
        if self.selected_slot is None:
            return self
        selected = self.selected_slot
        if (
            selected.offer_id != self.offer.offer_id
            or selected.business_id != self.offer.business_id
            or selected.conversation_id != self.offer.conversation_id
            or selected.service_id != self.offer.service_id
            or selected.availability_revision != self.offer.availability_revision
            or not any(slot == selected for slot in self.offer.slots)
        ):
            raise ValueError("Selected slot does not belong to active offer")
        return self

    def serialize(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def deserialize(cls, value: object) -> AvailabilitySelectionState:
        if not isinstance(value, dict):
            raise ValueError("Availability selection state must be an object")
        try:
            return cls.model_validate_json(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
        except ValidationError as exc:
            raise ValueError("Availability selection state is invalid") from exc


@dataclass(frozen=True, slots=True)
class OfferSelection:
    status: OfferSelectionStatus
    slot: SelectedSlotRef | None = None


def _canonical_slot_values(slots: tuple[SelectedSlotRef, ...]) -> list[dict[str, object]]:
    return [
        {
            "ordinal": slot.ordinal,
            "resource_id": slot.resource_id,
            "resource_name": slot.resource_name,
            "start_at": slot.start_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end_at": slot.end_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        for slot in slots
    ]


def availability_revision_for_slots(
    *,
    business_id: int,
    conversation_id: str,
    service_id: int,
    target_date: date,
    business_timezone: str,
    slots: tuple[SelectedSlotRef, ...],
) -> str:
    payload = {
        "domain": "fonely.availability-offer.v1",
        "business_id": business_id,
        "conversation_id": conversation_id,
        "service_id": service_id,
        "target_date": target_date.isoformat(),
        "business_timezone": business_timezone,
        "slots": _canonical_slot_values(slots),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_availability_offer(
    *,
    business_id: int,
    conversation_id: str,
    service_id: int,
    business_timezone: str,
    alternatives: tuple[AvailableSlot, ...],
    now: datetime,
    ttl: timedelta = _DEFAULT_OFFER_TTL,
) -> AvailabilityOffer:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Offer clock must be timezone-aware")
    if not alternatives or len(alternatives) > _MAX_OFFER_SLOTS:
        raise ValueError("Offer alternatives count is invalid")
    if ttl <= timedelta(0) or ttl > timedelta(minutes=15):
        raise ValueError("Offer TTL is invalid")

    offer_id = secrets.token_urlsafe(16)
    expires_at = now + ttl
    provisional = tuple(
        SelectedSlotRef(
            slot_token=secrets.token_urlsafe(16),
            offer_id=offer_id,
            business_id=business_id,
            conversation_id=conversation_id,
            service_id=service_id,
            resource_id=slot.resource_id,
            resource_name=slot.resource_name,
            start_at=slot.start_at,
            end_at=slot.end_at,
            business_timezone=business_timezone,
            ordinal=index,
            expires_at=expires_at,
            availability_revision="0" * 64,
        )
        for index, slot in enumerate(alternatives, start=1)
    )
    target_date = provisional[0].start_at.astimezone(ZoneInfo(business_timezone)).date()
    revision = availability_revision_for_slots(
        business_id=business_id,
        conversation_id=conversation_id,
        service_id=service_id,
        target_date=target_date,
        business_timezone=business_timezone,
        slots=provisional,
    )
    slots = tuple(
        slot.model_copy(update={"availability_revision": revision}) for slot in provisional
    )
    return AvailabilityOffer(
        offer_id=offer_id,
        business_id=business_id,
        conversation_id=conversation_id,
        service_id=service_id,
        target_date=target_date,
        business_timezone=business_timezone,
        issued_at=now,
        expires_at=expires_at,
        availability_revision=revision,
        slots=slots,
    )


def select_from_offer(
    offer: AvailabilityOffer,
    user_text: str,
    *,
    now: datetime,
) -> OfferSelection:
    if now.tzinfo is None or now.utcoffset() is None:
        return OfferSelection(OfferSelectionStatus.INVALID)
    if instant(now) >= instant(offer.expires_at):
        return OfferSelection(OfferSelectionStatus.EXPIRED)

    raw_selection = user_text.strip()
    normalized = " ".join(raw_selection.casefold().split())
    token_matches = [
        slot for slot in offer.slots if secrets.compare_digest(slot.slot_token, raw_selection)
    ]
    if len(token_matches) == 1:
        return OfferSelection(OfferSelectionStatus.SELECTED, token_matches[0])

    ordinal = _ORDINALS.get(normalized)
    if ordinal is None and normalized.isdigit():
        ordinal = int(normalized)
    if ordinal is not None:
        matches = [slot for slot in offer.slots if slot.ordinal == ordinal]
        if len(matches) == 1:
            return OfferSelection(OfferSelectionStatus.SELECTED, matches[0])
        return OfferSelection(OfferSelectionStatus.NO_MATCH)

    match = _TIME_PATTERN.fullmatch(normalized)
    if match is None:
        return OfferSelection(OfferSelectionStatus.NO_MATCH)
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    ampm = match.group(3)
    if minute > 59 or hour < 1 or hour > 12:
        return OfferSelection(OfferSelectionStatus.NO_MATCH)

    def matches_time(slot: SelectedSlotRef) -> bool:
        local = slot.start_at.astimezone(ZoneInfo(offer.business_timezone))
        if ampm:
            expected_hour = hour % 12 + (12 if ampm.casefold() == "pm" else 0)
            return local.hour == expected_hour and local.minute == minute
        return local.hour % 12 == hour % 12 and local.minute == minute

    candidates = [slot for slot in offer.slots if matches_time(slot)]
    if len(candidates) == 1:
        return OfferSelection(OfferSelectionStatus.SELECTED, candidates[0])
    if len(candidates) > 1:
        return OfferSelection(OfferSelectionStatus.AMBIGUOUS)
    return OfferSelection(OfferSelectionStatus.NO_MATCH)
