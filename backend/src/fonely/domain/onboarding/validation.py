"""Deterministic total validation engine for onboarding drafts."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fonely.domain.onboarding.enums import (
    IssueCategory,
    IssueSeverity,
    PriceKind,
    ReviewStatus,
)
from fonely.domain.onboarding.limits import MAX_ISSUES, SCHEMA_VERSION
from fonely.domain.onboarding.models import (
    BusinessOnboardingDraft,
    PricePolicy,
    SchedulePeriod,
    WeeklySchedule,
    effective_timezone,
)
from fonely.domain.onboarding.results import ValidationIssue, ValidationResult


def validate_draft(draft: BusinessOnboardingDraft) -> ValidationResult:
    issues: list[ValidationIssue] = []

    _validate_schema(draft, issues)
    _validate_business(draft, issues)
    _validate_locations(draft, issues)
    _validate_services(draft, issues)
    _validate_resources(draft, issues)
    _validate_products(draft, issues)
    _validate_policy(draft, issues)
    _validate_provenance(draft, issues)

    issues.sort(key=_issue_sort_key)
    if len(issues) > MAX_ISSUES:
        issues = issues[:MAX_ISSUES]

    blockers = tuple(i for i in issues if i.severity is IssueSeverity.BLOCKER)
    warnings = tuple(i for i in issues if i.severity is IssueSeverity.WARNING)

    return ValidationResult(
        issues=tuple(issues),
        blockers=blockers,
        warnings=warnings,
        blocker_count=len(blockers),
        warning_count=len(warnings),
        draft_digest=draft.canonical_digest(),
    )


def _issue_sort_key(issue: ValidationIssue) -> tuple[int, str, str]:
    severity_order = 0 if issue.severity is IssueSeverity.BLOCKER else 1
    return (severity_order, issue.path, issue.code)


def _add(
    issues: list[ValidationIssue],
    *,
    path: str,
    code: str,
    severity: IssueSeverity,
    category: IssueCategory,
    message: str,
) -> None:
    issues.append(
        ValidationIssue(
            path=path,
            code=code,
            severity=severity,
            category=category,
            message=message,
        )
    )


def _validate_schema(draft: BusinessOnboardingDraft, issues: list[ValidationIssue]) -> None:
    if draft.schema_version != SCHEMA_VERSION:
        _add(
            issues,
            path="schema_version",
            code="unsupported_schema",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.UNSUPPORTED,
            message=f"Schema version {draft.schema_version} is not supported",
        )


def _validate_business(draft: BusinessOnboardingDraft, issues: list[ValidationIssue]) -> None:
    if not draft.business_name:
        _add(
            issues,
            path="business_name",
            code="missing_business_name",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.MISSING,
            message="Business name is required",
        )
    if draft.default_timezone:
        if not _valid_timezone(draft.default_timezone):
            _add(
                issues,
                path="default_timezone",
                code="invalid_timezone",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.INVALID,
                message=f"Invalid timezone: {draft.default_timezone}",
            )
    else:
        _add(
            issues,
            path="default_timezone",
            code="missing_timezone",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.MISSING,
            message="Default timezone is required",
        )
    if len(draft.default_currency) != 3:
        _add(
            issues,
            path="default_currency",
            code="invalid_currency",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.INVALID,
            message="Currency must be a 3-letter code",
        )
    active_locations = [loc for loc in draft.locations if loc.is_active]
    if not active_locations:
        _add(
            issues,
            path="locations",
            code="no_active_location",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.MISSING,
            message="At least one active location is required",
        )


def _validate_locations(draft: BusinessOnboardingDraft, issues: list[ValidationIssue]) -> None:
    seen_keys: set[str] = set()
    for index, loc in enumerate(draft.locations):
        path = f"locations[{index}]"
        if loc.key in seen_keys:
            _add(
                issues,
                path=path,
                code="duplicate_location_key",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.DUPLICATE,
                message=f"Duplicate location key: {loc.key}",
            )
        seen_keys.add(loc.key)
        tz = effective_timezone(draft, loc)
        if tz and not _valid_timezone(tz):
            _add(
                issues,
                path=f"{path}.timezone_override",
                code="invalid_location_timezone",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.INVALID,
                message=f"Invalid timezone on location {loc.key}: {tz}",
            )
        _validate_schedule(loc.schedule, f"{path}.schedule", issues)


def _validate_schedule(schedule: WeeklySchedule, path: str, issues: list[ValidationIssue]) -> None:
    for index, period in enumerate(schedule.periods):
        _validate_period(period, f"{path}.periods[{index}]", issues)
    seen_days: dict[int, list[SchedulePeriod]] = {}
    for period in schedule.periods:
        if period.is_closed:
            continue
        seen_days.setdefault(period.day.value, []).append(period)
    for day_val, periods in seen_days.items():
        if len(periods) > 1:
            sorted_periods = sorted(periods, key=lambda p: p.start)
            for i in range(len(sorted_periods) - 1):
                if sorted_periods[i].end > sorted_periods[i + 1].start:
                    _add(
                        issues,
                        path=f"{path}.periods",
                        code="schedule_overlap",
                        severity=IssueSeverity.BLOCKER,
                        category=IssueCategory.INVALID,
                        message=f"Overlapping schedule periods on day {day_val}",
                    )
                    break


def _validate_period(period: SchedulePeriod, path: str, issues: list[ValidationIssue]) -> None:
    pass


def _validate_services(draft: BusinessOnboardingDraft, issues: list[ValidationIssue]) -> None:
    location_keys = {loc.key for loc in draft.locations}
    resource_keys = {res.key for res in draft.resources}
    seen_keys: set[str] = set()
    seen_names: set[str] = set()

    for index, svc in enumerate(draft.services):
        path = f"services[{index}]"
        if svc.key in seen_keys:
            _add(
                issues,
                path=path,
                code="duplicate_service_key",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.DUPLICATE,
                message=f"Duplicate service key: {svc.key}",
            )
        seen_keys.add(svc.key)
        normalized = svc.name.strip().lower()
        if normalized in seen_names:
            _add(
                issues,
                path=path,
                code="duplicate_service_name",
                severity=IssueSeverity.WARNING,
                category=IssueCategory.DUPLICATE,
                message=f"Possible duplicate service name: {svc.name}",
            )
        seen_names.add(normalized)
        for loc_key in svc.location_keys:
            if loc_key not in location_keys:
                _add(
                    issues,
                    path=f"{path}.location_keys",
                    code="invalid_service_location_ref",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.CROSS_REFERENCE,
                    message=f"Service {svc.key} references missing location {loc_key}",
                )
        for res_key in svc.eligible_resource_keys:
            if res_key not in resource_keys:
                _add(
                    issues,
                    path=f"{path}.eligible_resource_keys",
                    code="invalid_service_resource_ref",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.CROSS_REFERENCE,
                    message=f"Service {svc.key} references missing resource {res_key}",
                )
        if svc.is_active and svc.requires_resource and not svc.eligible_resource_keys:
            _add(
                issues,
                path=path,
                code="service_no_eligible_resource",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.MISSING,
                message=f"Active service {svc.key} requires a resource but has none",
            )
        if svc.is_active and svc.duration_minutes is None:
            _add(
                issues,
                path=f"{path}.duration_minutes",
                code="missing_service_duration",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.MISSING,
                message=f"Active service {svc.key} has no duration",
            )
        if svc.price is not None:
            _validate_price(svc.price, f"{path}.price", draft.default_currency, issues)
        elif svc.is_active:
            _add(
                issues,
                path=f"{path}.price",
                code="missing_service_price",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.MISSING,
                message=f"Active service {svc.key} has no price",
            )


def _validate_resources(draft: BusinessOnboardingDraft, issues: list[ValidationIssue]) -> None:
    location_keys = {loc.key for loc in draft.locations}
    service_keys = {svc.key for svc in draft.services}
    seen_keys: set[str] = set()

    for index, res in enumerate(draft.resources):
        path = f"resources[{index}]"
        if res.key in seen_keys:
            _add(
                issues,
                path=path,
                code="duplicate_resource_key",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.DUPLICATE,
                message=f"Duplicate resource key: {res.key}",
            )
        seen_keys.add(res.key)
        for loc_key in res.location_keys:
            if loc_key not in location_keys:
                _add(
                    issues,
                    path=f"{path}.location_keys",
                    code="invalid_resource_location_ref",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.CROSS_REFERENCE,
                    message=f"Resource {res.key} references missing location {loc_key}",
                )
        for svc_key in res.service_keys:
            if svc_key not in service_keys:
                _add(
                    issues,
                    path=f"{path}.service_keys",
                    code="invalid_resource_service_ref",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.CROSS_REFERENCE,
                    message=f"Resource {res.key} references missing service {svc_key}",
                )
        _validate_schedule(res.schedule, f"{path}.schedule", issues)


def _validate_products(draft: BusinessOnboardingDraft, issues: list[ValidationIssue]) -> None:
    location_keys = {loc.key for loc in draft.locations}
    seen_keys: set[str] = set()
    seen_names: set[str] = set()

    for index, prod in enumerate(draft.products):
        path = f"products[{index}]"
        if prod.key in seen_keys:
            _add(
                issues,
                path=path,
                code="duplicate_product_key",
                severity=IssueSeverity.BLOCKER,
                category=IssueCategory.DUPLICATE,
                message=f"Duplicate product key: {prod.key}",
            )
        seen_keys.add(prod.key)
        normalized = prod.name.strip().lower()
        if normalized in seen_names:
            _add(
                issues,
                path=path,
                code="duplicate_product_name",
                severity=IssueSeverity.WARNING,
                category=IssueCategory.DUPLICATE,
                message=f"Possible duplicate product name: {prod.name}",
            )
        seen_names.add(normalized)
        for loc_key in prod.location_keys:
            if loc_key not in location_keys:
                _add(
                    issues,
                    path=f"{path}.location_keys",
                    code="invalid_product_location_ref",
                    severity=IssueSeverity.BLOCKER,
                    category=IssueCategory.CROSS_REFERENCE,
                    message=f"Product {prod.key} references missing location {loc_key}",
                )
        if prod.price is not None:
            _validate_price(prod.price, f"{path}.price", draft.default_currency, issues)


def _validate_price(
    price: PricePolicy,
    path: str,
    expected_currency: str,
    issues: list[ValidationIssue],
) -> None:
    if price.currency != expected_currency:
        _add(
            issues,
            path=path,
            code="currency_mismatch",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.INVALID,
            message=f"Price currency {price.currency} does not match {expected_currency}",
        )
    if price.kind is PriceKind.NOT_PROVIDED:
        _add(
            issues,
            path=path,
            code="price_not_provided",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.MISSING,
            message="Price is required but not provided",
        )


def _validate_policy(draft: BusinessOnboardingDraft, issues: list[ValidationIssue]) -> None:
    policy = draft.policy
    if (
        policy.cancellation_cutoff_minutes is not None
        and policy.minimum_notice_minutes is not None
        and policy.cancellation_cutoff_minutes < policy.minimum_notice_minutes
    ):
        _add(
            issues,
            path="policy",
            code="incoherent_cancellation_notice",
            severity=IssueSeverity.WARNING,
            category=IssueCategory.INVALID,
            message="Cancellation cutoff is less than minimum notice",
        )


def _validate_provenance(draft: BusinessOnboardingDraft, issues: list[ValidationIssue]) -> None:
    if draft.business_name and draft.business_name_provenance.review_status is ReviewStatus.MISSING:
        pass
    if draft.business_name_provenance.review_status in {
        ReviewStatus.AMBIGUOUS,
        ReviewStatus.CONFLICTING,
    }:
        _add(
            issues,
            path="business_name_provenance",
            code="unresolved_business_name",
            severity=IssueSeverity.BLOCKER,
            category=_review_to_category(draft.business_name_provenance.review_status),
            message="Business name provenance is unresolved",
        )
    if draft.business_name_provenance.review_status is ReviewStatus.UNREADABLE:
        _add(
            issues,
            path="business_name_provenance",
            code="unreadable_business_name",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.UNREADABLE,
            message="Business name source is unreadable",
        )
    if draft.business_name_provenance.review_status is ReviewStatus.UNSUPPORTED:
        _add(
            issues,
            path="business_name_provenance",
            code="unsupported_business_name",
            severity=IssueSeverity.BLOCKER,
            category=IssueCategory.UNSUPPORTED,
            message="Business name source type is unsupported",
        )


def _review_to_category(status: ReviewStatus) -> IssueCategory:
    mapping = {
        ReviewStatus.AMBIGUOUS: IssueCategory.AMBIGUOUS,
        ReviewStatus.CONFLICTING: IssueCategory.CONFLICTING,
        ReviewStatus.UNREADABLE: IssueCategory.UNREADABLE,
        ReviewStatus.UNSUPPORTED: IssueCategory.UNSUPPORTED,
    }
    return mapping.get(status, IssueCategory.INVALID)


def _valid_timezone(tz: str) -> bool:
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, KeyError):
        return False
