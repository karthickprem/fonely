"""Deterministic total validation engine for onboarding drafts."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fonely.domain.onboarding.enums import (
    IssueCategory,
    IssueSeverity,
    PriceKind,
    ReviewStatus,
)
from fonely.domain.onboarding.limits import (
    MAX_SHORT_TEXT,
    SCHEMA_VERSION,
    SUPPORTED_CURRENCIES,
)
from fonely.domain.onboarding.models import (
    BusinessOnboardingDraft,
    FieldProvenance,
    PricePolicy,
    ProvenanceField,
    ResourceDraft,
    SchedulePeriod,
    WeeklySchedule,
    effective_timezone,
)
from fonely.domain.onboarding.results import ValidationIssue, ValidationResult

_UNRESOLVED_STATUSES = frozenset(
    {
        ReviewStatus.MISSING,
        ReviewStatus.AMBIGUOUS,
        ReviewStatus.CONFLICTING,
        ReviewStatus.UNREADABLE,
        ReviewStatus.UNSUPPORTED,
    }
)

_EVIDENCE_REQUIRED_STATUSES = frozenset(
    {
        ReviewStatus.CLEAR,
        ReviewStatus.OWNER_CONFIRMED,
        ReviewStatus.OWNER_CORRECTED,
    }
)

_BUSINESS_REQUIRED_PROV = (
    "business_name",
    "business_category",
    "default_timezone",
    "default_currency",
)

_LOCATION_REQUIRED_PROV = ("display_name", "is_active", "schedule")

_SERVICE_REQUIRED_PROV = (
    "name",
    "duration_minutes",
    "is_active",
    "price",
    "eligible_resource_keys",
    "requires_resource",
)

_RESOURCE_REQUIRED_PROV = (
    "display_name",
    "resource_type",
    "is_active",
    "location_keys",
    "service_keys",
    "schedule",
)

_PRODUCT_REQUIRED_PROV = ("name", "is_active")


def validate_draft(
    draft: BusinessOnboardingDraft,
) -> ValidationResult:
    blocker_count = 0
    warning_count = 0
    issues: list[ValidationIssue] = []
    max_detail = 500

    def add(
        *,
        path: str,
        code: str,
        severity: IssueSeverity,
        category: IssueCategory,
        message: str,
    ) -> None:
        nonlocal blocker_count, warning_count
        if severity is IssueSeverity.BLOCKER:
            blocker_count += 1
        else:
            warning_count += 1
        if len(issues) < max_detail:
            issues.append(
                ValidationIssue(
                    path=path,
                    code=code,
                    severity=severity,
                    category=category,
                    message=message,
                )
            )

    _validate_schema(draft, add)
    _validate_business(draft, add)
    _validate_locations(draft, add)
    _validate_services(draft, add)
    _validate_resources(draft, add)
    _validate_products(draft, add)
    _validate_policy(draft, add)
    _validate_all_provenance(draft, add)

    issues.sort(key=_issue_sort_key)
    omitted = (blocker_count + warning_count) - len(issues)

    blockers = tuple(i for i in issues if i.severity is IssueSeverity.BLOCKER)
    warnings = tuple(i for i in issues if i.severity is IssueSeverity.WARNING)

    return ValidationResult(
        issues=tuple(issues),
        blockers=blockers,
        warnings=warnings,
        blocker_count=blocker_count,
        warning_count=warning_count,
        omitted_issue_count=omitted,
        draft_digest=draft.canonical_digest(),
    )


def _issue_sort_key(
    issue: ValidationIssue,
) -> tuple[int, str, str]:
    severity_order = 0 if issue.severity is IssueSeverity.BLOCKER else 1
    return (severity_order, issue.path, issue.code)


def _validate_schema(draft: BusinessOnboardingDraft, add: Any) -> None:
    if draft.schema_version != SCHEMA_VERSION:
        add(
            path="schema_version",
            code="unsupported_schema",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.UNSUPPORTED,
            message=f"Schema version {draft.schema_version} is not supported",
        )


def _validate_business(draft: BusinessOnboardingDraft, add: Any) -> None:
    if not draft.business_name:
        add(
            path="business_name",
            code="missing_business_name",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.MISSING,
            message="Business name is required",
        )
    if draft.default_timezone:
        if not _valid_timezone(draft.default_timezone):
            add(
                path="default_timezone",
                code="invalid_timezone",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.INVALID,
                message=f"Invalid timezone: {draft.default_timezone}",
            )
    else:
        add(
            path="default_timezone",
            code="missing_timezone",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.MISSING,
            message="Default timezone is required",
        )
    if draft.default_currency.upper() not in SUPPORTED_CURRENCIES:
        add(
            path="default_currency",
            code="unsupported_currency",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.INVALID,
            message=f"Unsupported currency: {draft.default_currency}",
        )
    active_locations = [loc for loc in draft.locations if loc.is_active]
    if not active_locations:
        add(
            path="locations",
            code="no_active_location",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.MISSING,
            message="At least one active location is required",
        )


def _validate_locations(draft: BusinessOnboardingDraft, add: Any) -> None:
    seen_keys: set[str] = set()
    for index, loc in enumerate(draft.locations):
        path = f"locations[{index}]"
        if loc.key in seen_keys:
            add(
                path=path,
                code="duplicate_location_key",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.DUPLICATE,
                message=f"Duplicate location key: {loc.key}",
            )
        seen_keys.add(loc.key)
        tz = effective_timezone(draft, loc)
        if tz and not _valid_timezone(tz):
            add(
                path=f"{path}.timezone_override",
                code="invalid_location_timezone",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.INVALID,
                message=f"Invalid timezone on location {loc.key}: {tz}",
            )
        _validate_schedule(loc.schedule, f"{path}.schedule", add)


def _validate_schedule(schedule: WeeklySchedule, path: str, add: Any) -> None:
    seen_days: dict[int, list[SchedulePeriod]] = {}
    for period in schedule.periods:
        seen_days.setdefault(period.day.value, []).append(period)
    for day_val, periods in sorted(seen_days.items()):
        open_periods = [p for p in periods if not p.is_closed]
        closed_periods = [p for p in periods if p.is_closed]
        if open_periods and closed_periods:
            add(
                path=f"{path}.periods",
                code="schedule_closed_open_conflict",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.CONFLICTING,
                message=f"Day {day_val} has both open and closed periods",
            )
        if len(closed_periods) > 1:
            add(
                path=f"{path}.periods",
                code="schedule_duplicate_closed",
                severity=IssueSeverity.WARNING,
                category=IssueCategory.DUPLICATE,
                message=f"Day {day_val} has multiple closed markers",
            )
        if len(open_periods) > 1:
            sorted_open = sorted(open_periods, key=lambda p: p.start)
            for i in range(len(sorted_open) - 1):
                if sorted_open[i].end > sorted_open[i + 1].start:
                    add(
                        path=f"{path}.periods",
                        code="schedule_overlap",
                        severity=IssueSeverity.BLOCKER,
                        category=IssueCategory.INVALID,
                        message=f"Overlapping schedule periods on day {day_val}",
                    )
                    break


def _validate_services(draft: BusinessOnboardingDraft, add: Any) -> None:
    location_keys = {loc.key for loc in draft.locations}
    active_location_keys = {loc.key for loc in draft.locations if loc.is_active}
    resource_map = {res.key: res for res in draft.resources}
    seen_keys: set[str] = set()
    seen_names: set[str] = set()

    for index, svc in enumerate(draft.services):
        path = f"services[{index}]"
        if svc.key in seen_keys:
            add(
                path=path,
                code="duplicate_service_key",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.DUPLICATE,
                message=f"Duplicate service key: {svc.key}",
            )
        seen_keys.add(svc.key)
        normalized = svc.name.strip().lower()
        if normalized in seen_names:
            add(
                path=path,
                code="duplicate_service_name",
                severity=IssueSeverity.WARNING,
                category=IssueCategory.DUPLICATE,
                message=f"Possible duplicate service name: {svc.name}",
            )
        seen_names.add(normalized)
        for ki, loc_key in enumerate(svc.location_keys):
            if loc_key not in location_keys:
                add(
                    path=f"{path}.location_keys[{ki}]",
                    code="invalid_service_location_ref",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.CROSS_REFERENCE,
                    message=f"Service {svc.key} references missing location {loc_key}",
                )
        for ri, res_key in enumerate(svc.eligible_resource_keys):
            if res_key not in resource_map:
                add(
                    path=f"{path}.eligible_resource_keys[{ri}]",
                    code="invalid_service_resource_ref",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.CROSS_REFERENCE,
                    message=f"Service {svc.key} references missing resource {res_key}",
                )
        if svc.is_active and svc.requires_resource:
            if not svc.eligible_resource_keys or not svc.location_keys:
                add(
                    path=path,
                    code="service_no_usable_resource",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.MISSING,
                    message=f"Service {svc.key} needs explicit resource and location",
                )
            else:
                usable = _find_usable_resources(
                    svc.key,
                    svc.eligible_resource_keys,
                    svc.location_keys,
                    resource_map,
                    active_location_keys,
                )
                if not usable:
                    add(
                        path=path,
                        code="service_no_usable_resource",
                        severity=IssueSeverity.BLOCKER,
                        category=IssueCategory.MISSING,
                        message=f"Active service {svc.key} has no usable eligible resource",
                    )
        if svc.is_active and svc.duration_minutes is None:
            add(
                path=f"{path}.duration_minutes",
                code="missing_service_duration",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.MISSING,
                message=f"Active service {svc.key} has no duration",
            )
        if svc.price is not None:
            _validate_price(
                svc.price,
                f"{path}.price",
                draft.default_currency,
                add,
            )
        elif svc.is_active:
            add(
                path=f"{path}.price",
                code="missing_service_price",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.MISSING,
                message=f"Active service {svc.key} has no price",
            )


def _find_usable_resources(
    service_key: str,
    eligible_keys: tuple[str, ...],
    service_location_keys: tuple[str, ...],
    resource_map: dict[str, ResourceDraft],
    active_location_keys: set[str],
) -> list[str]:
    usable = []
    service_locs = set(service_location_keys)
    for rk in eligible_keys:
        res = resource_map.get(rk)
        if res is None or not res.is_active:
            continue
        if not res.location_keys:
            continue
        res_locs = set(res.location_keys)
        if not (res_locs & service_locs & active_location_keys):
            continue
        if not res.service_keys:
            continue
        if service_key not in set(res.service_keys):
            continue
        usable.append(rk)
    return usable


def _validate_resources(draft: BusinessOnboardingDraft, add: Any) -> None:
    location_keys = {loc.key for loc in draft.locations}
    service_keys = {svc.key for svc in draft.services}
    seen_keys: set[str] = set()

    for index, res in enumerate(draft.resources):
        path = f"resources[{index}]"
        if res.key in seen_keys:
            add(
                path=path,
                code="duplicate_resource_key",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.DUPLICATE,
                message=f"Duplicate resource key: {res.key}",
            )
        seen_keys.add(res.key)
        for ki, loc_key in enumerate(res.location_keys):
            if loc_key not in location_keys:
                add(
                    path=f"{path}.location_keys[{ki}]",
                    code="invalid_resource_location_ref",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.CROSS_REFERENCE,
                    message=f"Resource {res.key} references missing location {loc_key}",
                )
        for si, svc_key in enumerate(res.service_keys):
            if svc_key not in service_keys:
                add(
                    path=f"{path}.service_keys[{si}]",
                    code="invalid_resource_service_ref",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.CROSS_REFERENCE,
                    message=f"Resource {res.key} references missing service {svc_key}",
                )
        _validate_schedule(res.schedule, f"{path}.schedule", add)


def _validate_products(draft: BusinessOnboardingDraft, add: Any) -> None:
    location_keys = {loc.key for loc in draft.locations}
    seen_keys: set[str] = set()
    seen_names: set[str] = set()

    for index, prod in enumerate(draft.products):
        path = f"products[{index}]"
        if prod.key in seen_keys:
            add(
                path=path,
                code="duplicate_product_key",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.DUPLICATE,
                message=f"Duplicate product key: {prod.key}",
            )
        seen_keys.add(prod.key)
        normalized = prod.name.strip().lower()
        if normalized in seen_names:
            add(
                path=path,
                code="duplicate_product_name",
                severity=IssueSeverity.WARNING,
                category=IssueCategory.DUPLICATE,
                message=f"Possible duplicate product name: {prod.name}",
            )
        seen_names.add(normalized)
        for ki, loc_key in enumerate(prod.location_keys):
            if loc_key not in location_keys:
                add(
                    path=f"{path}.location_keys[{ki}]",
                    code="invalid_product_location_ref",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.CROSS_REFERENCE,
                    message=f"Product {prod.key} references missing location {loc_key}",
                )
        if prod.price is not None:
            _validate_price(
                prod.price,
                f"{path}.price",
                draft.default_currency,
                add,
            )


def _validate_price(
    price: PricePolicy,
    path: str,
    expected_currency: str,
    add: Any,
) -> None:
    if price.currency.upper() != expected_currency.upper():
        add(
            path=path,
            code="currency_mismatch",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.INVALID,
            message=f"Price currency {price.currency} does not match {expected_currency}",
        )
    if price.kind is PriceKind.NOT_PROVIDED:
        add(
            path=path,
            code="price_not_provided",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.MISSING,
            message="Price is required but not provided",
        )


def _validate_policy(draft: BusinessOnboardingDraft, add: Any) -> None:
    policy = draft.policy
    if (
        policy.cancellation_cutoff_minutes is not None
        and policy.minimum_notice_minutes is not None
        and policy.cancellation_cutoff_minutes < policy.minimum_notice_minutes
    ):
        add(
            path="policy",
            code="incoherent_cancellation_notice",
            severity=IssueSeverity.WARNING,
            category=IssueCategory.INVALID,
            message="Cancellation cutoff is less than minimum notice",
        )


def _validate_all_provenance(draft: BusinessOnboardingDraft, add: Any) -> None:
    for field_name in _BUSINESS_REQUIRED_PROV:
        prov = draft.provenance.get(field_name)
        _check_provenance(prov, f"provenance.{field_name}", add)
    _check_supplied_contact_prov(draft.contact, draft.provenance, "contact", add)
    _check_supplied_policy_prov(draft.policy, add)

    for idx, loc in enumerate(draft.locations):
        for fn in _LOCATION_REQUIRED_PROV:
            prov = loc.provenance.get(fn)
            _check_provenance(
                prov,
                f"locations[{idx}].provenance.{fn}",
                add,
            )
        _check_supplied_contact_prov(
            loc.contact,
            loc.provenance,
            f"locations[{idx}].contact",
            add,
        )
        _check_supplied_address_prov(
            loc.address,
            loc.provenance,
            f"locations[{idx}].address",
            add,
        )
        if loc.timezone_override:
            prov = loc.provenance.get("timezone_override")
            _check_provenance(
                prov,
                f"locations[{idx}].provenance.timezone_override",
                add,
            )

    for idx, svc in enumerate(draft.services):
        for fn in _SERVICE_REQUIRED_PROV:
            prov = svc.provenance.get(fn)
            _check_provenance(
                prov,
                f"services[{idx}].provenance.{fn}",
                add,
            )
        if svc.location_keys:
            prov = svc.provenance.get("location_keys")
            _check_provenance(
                prov,
                f"services[{idx}].provenance.location_keys",
                add,
            )
        if svc.buffer_before_minutes > 0:
            prov = svc.provenance.get("buffer_before_minutes")
            _check_provenance(
                prov,
                f"services[{idx}].provenance.buffer_before_minutes",
                add,
            )
        if svc.buffer_after_minutes > 0:
            prov = svc.provenance.get("buffer_after_minutes")
            _check_provenance(
                prov,
                f"services[{idx}].provenance.buffer_after_minutes",
                add,
            )

    for idx, res in enumerate(draft.resources):
        for fn in _RESOURCE_REQUIRED_PROV:
            prov = res.provenance.get(fn)
            _check_provenance(
                prov,
                f"resources[{idx}].provenance.{fn}",
                add,
            )

    for idx, prod in enumerate(draft.products):
        for fn in _PRODUCT_REQUIRED_PROV:
            prov = prod.provenance.get(fn)
            _check_provenance(
                prov,
                f"products[{idx}].provenance.{fn}",
                add,
            )
        if prod.price is not None:
            prov = prod.provenance.get("price")
            _check_provenance(
                prov,
                f"products[{idx}].provenance.price",
                add,
            )
        if prod.unit is not None:
            prov = prod.provenance.get("unit")
            _check_provenance(
                prov,
                f"products[{idx}].provenance.unit",
                add,
            )
        if prod.location_keys:
            prov = prod.provenance.get("location_keys")
            _check_provenance(
                prov,
                f"products[{idx}].provenance.location_keys",
                add,
            )


def _check_supplied_contact_prov(
    contact: Any,
    provenance: FieldProvenance,
    report_prefix: str,
    add: Any,
) -> None:
    for field in ("phone", "email", "website"):
        val = getattr(contact, field, None)
        if val:
            prov = provenance.get(f"contact.{field}")
            _check_provenance(
                prov,
                f"{report_prefix}.{field}",
                add,
            )


def _check_supplied_address_prov(
    address: Any,
    provenance: FieldProvenance,
    report_prefix: str,
    add: Any,
) -> None:
    for field in ("line1", "city", "state", "pincode"):
        val = getattr(address, field, None)
        if val:
            prov = provenance.get(f"address.{field}")
            _check_provenance(
                prov,
                f"{report_prefix}.{field}",
                add,
            )


def _check_supplied_policy_prov(
    policy: Any,
    add: Any,
) -> None:
    prov_map = policy.provenance
    for field in (
        "advance_booking_days",
        "minimum_notice_minutes",
        "cancellation_cutoff_minutes",
        "rescheduling_allowed",
        "walk_in_allowed",
        "resource_selection_required",
    ):
        val = getattr(policy, field, None)
        if val is not None:
            prov = prov_map.get(field)
            _check_provenance(
                prov,
                f"policy.provenance.{field}",
                add,
            )


def _check_provenance(
    prov: ProvenanceField | None,
    path: str,
    add: Any,
) -> None:
    if prov is None:
        add(
            path=path,
            code="missing_field_provenance",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.MISSING,
            message=f"Required field provenance missing at {path}",
        )
        return
    if prov.review_status in _UNRESOLVED_STATUSES:
        add(
            path=path,
            code=f"unresolved_{prov.review_status.value}",
            severity=IssueSeverity.BLOCKER,
            category=_review_to_category(prov.review_status),
            message=f"Field at {path} has unresolved status: {prov.review_status.value}",
        )
    if prov.review_status in _EVIDENCE_REQUIRED_STATUSES and not prov.has_valid_evidence():
        add(
            path=path,
            code="no_evidence",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.MISSING,
            message=f"Field at {path} claims {prov.review_status.value} but has no valid evidence",
        )


def _review_to_category(
    status: ReviewStatus,
) -> IssueCategory:
    mapping = {
        ReviewStatus.MISSING: IssueCategory.MISSING,
        ReviewStatus.AMBIGUOUS: IssueCategory.AMBIGUOUS,
        ReviewStatus.CONFLICTING: IssueCategory.CONFLICTING,
        ReviewStatus.UNREADABLE: IssueCategory.UNREADABLE,
        ReviewStatus.UNSUPPORTED: IssueCategory.UNSUPPORTED,
    }
    return mapping.get(status, IssueCategory.INVALID)


def _valid_timezone(tz: str) -> bool:
    if not tz or len(tz) > MAX_SHORT_TEXT:
        return False
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return False
