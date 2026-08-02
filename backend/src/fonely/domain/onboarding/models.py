"""Canonical business onboarding draft models."""

from __future__ import annotations

import hashlib
import json
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

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
    MAX_KEY_LENGTH,
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
    normalize_currency,
    validate_key_element,
)

ShortText = Annotated[str, Field(min_length=1, max_length=MAX_SHORT_TEXT)]


def _validate_entity_key(v: str) -> str:
    return validate_key_element(v, "key")


BoundedKey = Annotated[
    str, Field(min_length=1, max_length=MAX_KEY_LENGTH), AfterValidator(_validate_entity_key)
]

_DIGEST_EXCLUDES = frozenset({"created_at", "updated_at", "source_batches", "status"})


def _validate_key_tuple(values: tuple[str, ...], field: str, limit: int) -> tuple[str, ...]:
    if len(values) > limit:
        raise DraftLimitExceededError(field, limit, len(values))
    return tuple(validate_key_element(v, field) for v in values)


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: SourceType
    source_id: str = Field(min_length=1, max_length=MAX_SHORT_TEXT)
    locator: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    batch_id: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    adapter_version: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    owner_provided: bool = False

    @model_validator(mode="after")
    def _validate_source_id(self) -> SourceReference:
        if not self.source_id.strip():
            raise ValueError("source_id must not be whitespace-only")
        return self


class ProvenanceField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_status: ReviewStatus = ReviewStatus.MISSING
    sources: tuple[SourceReference, ...] = ()
    resolved_value: str | None = Field(default=None, max_length=MAX_LONG_TEXT)

    @model_validator(mode="after")
    def _check_sources_limit(self) -> ProvenanceField:
        if len(self.sources) > MAX_PROVENANCE_ENTRIES:
            raise DraftLimitExceededError(
                "provenance_sources",
                MAX_PROVENANCE_ENTRIES,
                len(self.sources),
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

    def has_valid_evidence(self) -> bool:
        return len(self.sources) > 0 and all(s.source_id.strip() for s in self.sources)


class FieldProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: tuple[tuple[str, ProvenanceField], ...] = ()

    @model_validator(mode="after")
    def _check_limits(self) -> FieldProvenance:
        if len(self.fields) > MAX_PROVENANCE_PATHS:
            raise DraftLimitExceededError(
                "field_provenance_paths",
                MAX_PROVENANCE_PATHS,
                len(self.fields),
            )
        seen: set[str] = set()
        for path, _ in self.fields:
            stripped = path.strip()
            if not stripped or len(stripped) > MAX_SHORT_TEXT:
                raise ValueError(f"Invalid provenance path: {path!r}")
            if stripped in seen:
                raise ValueError(f"Duplicate provenance path: {stripped}")
            seen.add(stripped)
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
        elif kind in {
            PriceKind.VARIABLE,
            PriceKind.CONSULTATION_REQUIRED,
        }:
            if self.amount is not None or self.minimum is not None or self.maximum is not None:
                raise ValueError(f"{kind.value} price must not specify amounts")
        elif kind is PriceKind.NOT_PROVIDED:
            if self.amount is not None or self.minimum is not None or self.maximum is not None:
                raise ValueError("not_provided price must not specify amounts")
        normalize_currency(self.currency)
        return self


class SchedulePeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    day: Weekday
    start: time
    end: time
    is_closed: bool = False

    @model_validator(mode="after")
    def _validate_period(self) -> SchedulePeriod:
        if self.is_closed:
            if self.start != time(0, 0) or self.end != time(0, 0):
                raise ValueError("closed period must use start=00:00 end=00:00")
        elif self.start >= self.end:
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
        if self.is_closed:
            if self.start is not None or self.end is not None:
                raise ValueError("closed exception must not specify start or end")
        else:
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
                "schedule_periods",
                MAX_SCHEDULE_PERIODS,
                len(self.periods),
            )
        if len(self.exceptions) > MAX_SCHEDULE_EXCEPTIONS:
            raise DraftLimitExceededError(
                "schedule_exceptions",
                MAX_SCHEDULE_EXCEPTIONS,
                len(self.exceptions),
            )
        exc_dates: dict[date, int] = {}
        for exc in self.exceptions:
            exc_dates[exc.date] = exc_dates.get(exc.date, 0) + 1
        for d, count in exc_dates.items():
            if count > 1:
                raise ValueError(f"Duplicate exception date: {d}")
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

    key: BoundedKey
    display_name: ShortText
    address: AddressComponents = Field(default_factory=AddressComponents)
    timezone_override: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    contact: ContactInfo = Field(default_factory=ContactInfo)
    is_active: bool = True
    schedule: WeeklySchedule = Field(default_factory=WeeklySchedule)
    provenance: FieldProvenance = Field(default_factory=FieldProvenance)

    @model_validator(mode="after")
    def _validate_tz(self) -> LocationDraft:
        if self.timezone_override is not None:
            stripped = self.timezone_override.strip()
            if not stripped:
                raise ValueError("explicit timezone_override must not be empty")
        return self


class ServiceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: BoundedKey
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
        _validate_key_tuple(
            self.location_keys,
            "service_location_keys",
            MAX_LOCATION_KEYS_PER_ENTITY,
        )
        _validate_key_tuple(
            self.eligible_resource_keys,
            "service_resource_keys",
            MAX_RESOURCE_KEYS_PER_SERVICE,
        )
        return self


class ResourceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: BoundedKey
    display_name: ShortText
    resource_type: ResourceType = ResourceType.STAFF
    location_keys: tuple[str, ...] = ()
    service_keys: tuple[str, ...] = ()
    schedule: WeeklySchedule = Field(default_factory=WeeklySchedule)
    is_active: bool = True
    provenance: FieldProvenance = Field(default_factory=FieldProvenance)

    @model_validator(mode="after")
    def _check_limits(self) -> ResourceDraft:
        _validate_key_tuple(
            self.service_keys,
            "resource_service_keys",
            MAX_SERVICE_KEYS_PER_RESOURCE,
        )
        _validate_key_tuple(
            self.location_keys,
            "resource_location_keys",
            MAX_LOCATION_KEYS_PER_ENTITY,
        )
        return self


class ProductDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: BoundedKey
    name: ShortText
    unit: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    category: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    price: PricePolicy | None = None
    is_active: bool = True
    location_keys: tuple[str, ...] = ()
    provenance: FieldProvenance = Field(default_factory=FieldProvenance)

    @model_validator(mode="after")
    def _check_limits(self) -> ProductDraft:
        _validate_key_tuple(
            self.location_keys,
            "product_location_keys",
            MAX_LOCATION_KEYS_PER_ENTITY,
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
    draft_id: BoundedKey
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
        _assert_limit(
            "preferred_languages",
            self.preferred_languages,
            MAX_LANGUAGES,
        )
        _assert_limit(
            "source_batches",
            self.source_batches,
            MAX_SOURCE_BATCHES,
        )
        for lang in self.preferred_languages:
            if not lang.strip() or len(lang) > MAX_SHORT_TEXT:
                raise ValueError(f"Invalid language identifier: {lang!r}")
        for sb in self.source_batches:
            if not sb.strip() or len(sb) > MAX_SHORT_TEXT:
                raise ValueError(f"Invalid source batch identifier: {sb!r}")
        return self

    def canonical_digest(self) -> str:
        return compute_canonical_digest(self)

    def with_updates(self, **kwargs: Any) -> BusinessOnboardingDraft:
        return BusinessOnboardingDraft.model_validate({**self.model_dump(mode="json"), **kwargs})


def _assert_limit(name: str, collection: tuple[Any, ...], limit: int) -> None:
    if len(collection) > limit:
        raise DraftLimitExceededError(name, limit, len(collection))


def _canonicalize_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _canonicalize_obj(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        items = [_canonicalize_obj(i) for i in obj]
        if items and all(isinstance(i, dict) for i in items):
            return sorted(
                items,
                key=lambda d: json.dumps(d, sort_keys=True, default=str),
            )
        if items and all(isinstance(i, (str, int, float)) for i in items):
            return sorted(items, key=str)
        return items
    return obj


def compute_canonical_digest(
    draft: BusinessOnboardingDraft,
) -> str:
    data = draft.model_dump(mode="json")
    for key in _DIGEST_EXCLUDES:
        data.pop(key, None)
    normalized = _canonicalize_obj(data)
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
