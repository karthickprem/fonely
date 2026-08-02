"""Canonical business onboarding draft models."""

from __future__ import annotations

import hashlib
import json
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fonely.domain.onboarding.enums import (
    BusinessCategory,
    DraftStatus,
    PriceKind,
    ResourceType,
    ReviewStatus,
    SourceType,
    Weekday,
)
from fonely.domain.onboarding.errors import DraftLimitExceededError
from fonely.domain.onboarding.limits import (
    MAX_LANGUAGES,
    MAX_LOCATION_KEYS_PER_ENTITY,
    MAX_LOCATIONS,
    MAX_LONG_TEXT,
    MAX_PRODUCTS,
    MAX_PROVENANCE_ENTRIES,
    MAX_PROVENANCE_PATHS,
    MAX_RESOURCE_KEYS_PER_SERVICE,
    MAX_RESOURCES,
    MAX_REVIEWER_REF,
    MAX_SCHEDULE_EXCEPTIONS,
    MAX_SCHEDULE_PERIODS,
    MAX_SERVICE_KEYS_PER_RESOURCE,
    MAX_SERVICES,
    MAX_SHORT_TEXT,
    MAX_SOURCE_BATCHES,
    SCHEMA_VERSION,
    SUPPORTED_CURRENCIES,
)

ShortText = Annotated[str, Field(min_length=1, max_length=MAX_SHORT_TEXT)]
OptionalText = Annotated[str, Field(max_length=MAX_LONG_TEXT)]

_DIGEST_EXCLUDES = frozenset({"created_at", "updated_at", "source_batches", "status"})


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: SourceType
    source_id: str = Field(min_length=1, max_length=MAX_SHORT_TEXT)
    locator: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    batch_id: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    adapter_version: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    owner_provided: bool = False


class ProvenanceField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_status: ReviewStatus = ReviewStatus.MISSING
    sources: tuple[SourceReference, ...] = ()
    resolved_value: str | None = Field(default=None, max_length=MAX_LONG_TEXT)

    @model_validator(mode="after")
    def _check_sources_limit(self) -> ProvenanceField:
        if len(self.sources) > MAX_PROVENANCE_ENTRIES:
            raise DraftLimitExceededError(
                "provenance_sources", MAX_PROVENANCE_ENTRIES, len(self.sources)
            )
        return self

    def with_correction(self, resolved: str, source: SourceReference) -> ProvenanceField:
        return ProvenanceField(
            review_status=ReviewStatus.OWNER_CORRECTED,
            sources=(*self.sources, source),
            resolved_value=resolved,
        )

    def with_confirmation(self, source: SourceReference) -> ProvenanceField:
        return ProvenanceField(
            review_status=ReviewStatus.OWNER_CONFIRMED,
            sources=(*self.sources, source),
            resolved_value=self.resolved_value,
        )

    def has_evidence(self) -> bool:
        return len(self.sources) > 0


class FieldProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: tuple[tuple[str, ProvenanceField], ...] = ()

    @model_validator(mode="after")
    def _check_limits(self) -> FieldProvenance:
        if len(self.fields) > MAX_PROVENANCE_PATHS:
            raise DraftLimitExceededError(
                "field_provenance_paths", MAX_PROVENANCE_PATHS, len(self.fields)
            )
        seen: set[str] = set()
        for path, _ in self.fields:
            if path in seen:
                raise ValueError(f"Duplicate provenance path: {path}")
            seen.add(path)
        return self

    def get(self, path: str) -> ProvenanceField | None:
        for p, prov in self.fields:
            if p == path:
                return prov
        return None

    def with_field(self, path: str, prov: ProvenanceField) -> FieldProvenance:
        entries = [(p, v) for p, v in self.fields if p != path]
        entries.append((path, prov))
        entries.sort(key=lambda e: e[0])
        return FieldProvenance(fields=tuple(entries))


class PricePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PriceKind
    currency: str = Field(min_length=3, max_length=3)
    amount: Decimal | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    note: str | None = Field(default=None, max_length=MAX_LONG_TEXT)
    provenance: ProvenanceField = Field(default_factory=ProvenanceField)

    @model_validator(mode="after")
    def _validate_price_semantics(self) -> PricePolicy:
        kind = self.kind
        if kind is PriceKind.FIXED:
            if self.amount is None:
                raise ValueError("fixed price requires amount")
            if self.amount < 0:
                raise ValueError("fixed price amount must be non-negative")
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("fixed price must not specify minimum or maximum")
        elif kind is PriceKind.STARTING_FROM:
            if self.minimum is None:
                raise ValueError("starting_from price requires minimum")
            if self.minimum < 0:
                raise ValueError("starting_from minimum must be non-negative")
            if self.amount is not None or self.maximum is not None:
                raise ValueError("starting_from must not specify amount or maximum")
        elif kind is PriceKind.RANGE:
            if self.minimum is None or self.maximum is None:
                raise ValueError("range price requires minimum and maximum")
            if self.minimum < 0 or self.maximum < 0:
                raise ValueError("range amounts must be non-negative")
            if self.minimum > self.maximum:
                raise ValueError("range minimum must not exceed maximum")
            if self.amount is not None:
                raise ValueError("range price must not specify amount")
        elif kind in {PriceKind.VARIABLE, PriceKind.CONSULTATION_REQUIRED}:
            if self.amount is not None or self.minimum is not None or self.maximum is not None:
                raise ValueError(f"{kind.value} price must not specify amounts")
        elif kind is PriceKind.NOT_PROVIDED:
            if self.amount is not None or self.minimum is not None or self.maximum is not None:
                raise ValueError("not_provided price must not specify amounts")
        if self.currency.upper() not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Unsupported currency: {self.currency}")
        return self


class SchedulePeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    day: Weekday
    start: time
    end: time
    is_closed: bool = False

    @model_validator(mode="after")
    def _validate_period(self) -> SchedulePeriod:
        if not self.is_closed and self.start >= self.end:
            raise ValueError("schedule start must be before end")
        return self


class ScheduleException(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    is_closed: bool = True
    start: time | None = None
    end: time | None = None
    reason: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)

    @model_validator(mode="after")
    def _validate_exception(self) -> ScheduleException:
        if not self.is_closed:
            if self.start is None or self.end is None:
                raise ValueError("open exception requires start and end")
            if self.start >= self.end:
                raise ValueError("exception start must be before end")
        return self


class WeeklySchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    periods: tuple[SchedulePeriod, ...] = ()
    exceptions: tuple[ScheduleException, ...] = ()

    @model_validator(mode="after")
    def _check_limits(self) -> WeeklySchedule:
        if len(self.periods) > MAX_SCHEDULE_PERIODS:
            raise DraftLimitExceededError(
                "schedule_periods", MAX_SCHEDULE_PERIODS, len(self.periods)
            )
        if len(self.exceptions) > MAX_SCHEDULE_EXCEPTIONS:
            raise DraftLimitExceededError(
                "schedule_exceptions", MAX_SCHEDULE_EXCEPTIONS, len(self.exceptions)
            )
        return self


class ContactInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phone: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    email: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    website: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)


class AddressComponents(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line1: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    line2: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    city: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    state: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    pincode: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    country: str = Field(default="IN", max_length=2)


class LocationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: ShortText
    display_name: ShortText
    address: AddressComponents = Field(default_factory=AddressComponents)
    timezone_override: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    contact: ContactInfo = Field(default_factory=ContactInfo)
    is_active: bool = True
    schedule: WeeklySchedule = Field(default_factory=WeeklySchedule)
    provenance: FieldProvenance = Field(default_factory=FieldProvenance)


class ServiceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: ShortText
    name: ShortText
    description: str | None = Field(default=None, max_length=MAX_LONG_TEXT)
    location_keys: tuple[str, ...] = ()
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    buffer_before_minutes: int = Field(default=0, ge=0, le=120)
    buffer_after_minutes: int = Field(default=0, ge=0, le=120)
    category: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    is_active: bool = True
    price: PricePolicy | None = None
    eligible_resource_keys: tuple[str, ...] = ()
    requires_resource: bool = True
    provenance: FieldProvenance = Field(default_factory=FieldProvenance)

    @model_validator(mode="after")
    def _check_limits(self) -> ServiceDraft:
        if len(self.location_keys) > MAX_LOCATION_KEYS_PER_ENTITY:
            raise DraftLimitExceededError(
                "service_location_keys", MAX_LOCATION_KEYS_PER_ENTITY, len(self.location_keys)
            )
        if len(self.eligible_resource_keys) > MAX_RESOURCE_KEYS_PER_SERVICE:
            raise DraftLimitExceededError(
                "service_resource_keys",
                MAX_RESOURCE_KEYS_PER_SERVICE,
                len(self.eligible_resource_keys),
            )
        return self


class ResourceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: ShortText
    display_name: ShortText
    resource_type: ResourceType = ResourceType.STAFF
    location_keys: tuple[str, ...] = ()
    service_keys: tuple[str, ...] = ()
    schedule: WeeklySchedule = Field(default_factory=WeeklySchedule)
    is_active: bool = True
    provenance: FieldProvenance = Field(default_factory=FieldProvenance)

    @model_validator(mode="after")
    def _check_limits(self) -> ResourceDraft:
        if len(self.service_keys) > MAX_SERVICE_KEYS_PER_RESOURCE:
            raise DraftLimitExceededError(
                "resource_service_keys",
                MAX_SERVICE_KEYS_PER_RESOURCE,
                len(self.service_keys),
            )
        if len(self.location_keys) > MAX_LOCATION_KEYS_PER_ENTITY:
            raise DraftLimitExceededError(
                "resource_location_keys",
                MAX_LOCATION_KEYS_PER_ENTITY,
                len(self.location_keys),
            )
        return self


class ProductDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: ShortText
    name: ShortText
    unit: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    category: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    price: PricePolicy | None = None
    is_active: bool = True
    location_keys: tuple[str, ...] = ()
    provenance: FieldProvenance = Field(default_factory=FieldProvenance)

    @model_validator(mode="after")
    def _check_limits(self) -> ProductDraft:
        if len(self.location_keys) > MAX_LOCATION_KEYS_PER_ENTITY:
            raise DraftLimitExceededError(
                "product_location_keys",
                MAX_LOCATION_KEYS_PER_ENTITY,
                len(self.location_keys),
            )
        return self


class PolicyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    advance_booking_days: int | None = Field(default=None, ge=0, le=365)
    minimum_notice_minutes: int | None = Field(default=None, ge=0, le=10080)
    cancellation_cutoff_minutes: int | None = Field(default=None, ge=0, le=10080)
    rescheduling_allowed: bool | None = None
    no_show_policy: str | None = Field(default=None, max_length=MAX_LONG_TEXT)
    walk_in_allowed: bool | None = None
    resource_selection_required: bool | None = None
    owner_review_required: bool = True
    provenance: FieldProvenance = Field(default_factory=FieldProvenance)


class BusinessOnboardingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    draft_id: ShortText
    status: DraftStatus = DraftStatus.INTAKE
    business_name: ShortText | None = None
    business_category: BusinessCategory | None = None
    default_timezone: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    default_currency: str = Field(default="INR", min_length=3, max_length=3)
    preferred_languages: tuple[str, ...] = ()
    contact: ContactInfo = Field(default_factory=ContactInfo)
    policy: PolicyDraft = Field(default_factory=PolicyDraft)
    locations: tuple[LocationDraft, ...] = ()
    services: tuple[ServiceDraft, ...] = ()
    resources: tuple[ResourceDraft, ...] = ()
    products: tuple[ProductDraft, ...] = ()
    source_batches: tuple[str, ...] = ()
    created_at: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    updated_at: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    provenance: FieldProvenance = Field(default_factory=FieldProvenance)

    @model_validator(mode="after")
    def _check_collection_limits(self) -> BusinessOnboardingDraft:
        _assert_limit("locations", self.locations, MAX_LOCATIONS)
        _assert_limit("services", self.services, MAX_SERVICES)
        _assert_limit("resources", self.resources, MAX_RESOURCES)
        _assert_limit("products", self.products, MAX_PRODUCTS)
        _assert_limit("preferred_languages", self.preferred_languages, MAX_LANGUAGES)
        _assert_limit("source_batches", self.source_batches, MAX_SOURCE_BATCHES)
        return self

    def canonical_digest(self) -> str:
        return compute_canonical_digest(self)

    def with_updates(self, **kwargs: Any) -> BusinessOnboardingDraft:
        return BusinessOnboardingDraft.model_validate({**self.model_dump(mode="json"), **kwargs})


def _assert_limit(name: str, collection: tuple[Any, ...], limit: int) -> None:
    if len(collection) > limit:
        raise DraftLimitExceededError(name, limit, len(collection))


def _normalize_for_digest(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _normalize_for_digest(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        items = [_normalize_for_digest(i) for i in obj]
        if items and isinstance(items[0], (str, int, float)):
            return sorted(items, key=str)
        return items
    return obj


def compute_canonical_digest(draft: BusinessOnboardingDraft) -> str:
    data = draft.model_dump(mode="json")
    for key in _DIGEST_EXCLUDES:
        data.pop(key, None)
    normalized = _normalize_for_digest(data)
    canonical = json.dumps(normalized, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def effective_timezone(draft: BusinessOnboardingDraft, location: LocationDraft) -> str | None:
    return location.timezone_override or draft.default_timezone


def effective_duration(service: ServiceDraft) -> timedelta | None:
    if service.duration_minutes is None:
        return None
    return timedelta(minutes=service.duration_minutes)


def validate_reviewer_ref(reviewer_ref: str) -> str:
    stripped = reviewer_ref.strip()
    if not stripped:
        raise ValueError("Reviewer reference must not be empty or whitespace")
    if len(stripped) > MAX_REVIEWER_REF:
        raise ValueError(f"Reviewer reference exceeds {MAX_REVIEWER_REF} characters")
    return stripped
