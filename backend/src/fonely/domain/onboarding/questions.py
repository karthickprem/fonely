"""Deterministic question planner over validation issues."""

from __future__ import annotations

from fonely.domain.onboarding.enums import IssueSeverity, QuestionAudience
from fonely.domain.onboarding.limits import MAX_QUESTIONS
from fonely.domain.onboarding.results import QuestionIntent, QuestionPlan, ValidationResult

_OWNER_CODES = frozenset(
    {
        "missing_business_name",
        "missing_timezone",
        "missing_service_duration",
        "missing_service_price",
        "price_not_provided",
        "currency_mismatch",
        "service_no_usable_resource",
        "missing_field_provenance",
        "no_evidence",
        "unresolved_missing",
        "unresolved_ambiguous",
        "unresolved_conflicting",
        "unresolved_unreadable",
        "unresolved_unsupported",
        "unsupported_currency",
    }
)

_INTENT_MAP: dict[str, str] = {
    "missing_business_name": "provide_business_name",
    "missing_timezone": "confirm_business_timezone",
    "invalid_timezone": "correct_timezone",
    "no_active_location": "add_active_location",
    "duplicate_location_key": "resolve_duplicate_location",
    "duplicate_service_key": "resolve_duplicate_service",
    "duplicate_service_name": "confirm_service_identity",
    "duplicate_resource_key": "resolve_duplicate_resource",
    "duplicate_product_key": "resolve_duplicate_product",
    "duplicate_product_name": "confirm_product_identity",
    "invalid_service_location_ref": "correct_service_location",
    "invalid_service_resource_ref": "correct_service_resource",
    "invalid_resource_location_ref": "correct_resource_location",
    "invalid_resource_service_ref": "correct_resource_service",
    "invalid_product_location_ref": "correct_product_location",
    "service_no_usable_resource": "assign_usable_resource_to_service",
    "missing_service_duration": "provide_service_duration",
    "missing_service_price": "provide_service_price",
    "price_not_provided": "classify_and_provide_price",
    "currency_mismatch": "correct_price_currency",
    "schedule_overlap": "resolve_schedule_overlap",
    "schedule_closed_open_conflict": "resolve_schedule_conflict",
    "exception_closed_open_conflict": "resolve_exception_conflict",
    "exception_duplicate_date": "resolve_duplicate_exception",
    "unsupported_schema": "upgrade_draft_schema",
    "invalid_location_timezone": "correct_location_timezone",
    "unsupported_currency": "correct_currency_code",
    "incoherent_cancellation_notice": "review_cancellation_policy",
    "missing_field_provenance": "provide_field_evidence",
    "no_evidence": "provide_field_evidence",
    "unresolved_missing": "provide_missing_value",
    "unresolved_ambiguous": "resolve_ambiguity",
    "unresolved_conflicting": "resolve_conflict",
    "unresolved_unreadable": "provide_value_manually",
    "unresolved_unsupported": "provide_value_manually",
}


def plan_questions(result: ValidationResult) -> QuestionPlan:
    seen: set[tuple[str, str]] = set()
    questions: list[QuestionIntent] = []
    priority = 0
    omitted = 0

    for issue in result.blockers:
        key = (issue.code, issue.path)
        if key in seen:
            continue
        seen.add(key)
        if len(questions) >= MAX_QUESTIONS:
            omitted += 1
            continue
        related = tuple(
            i.code for i in result.issues if i.path == issue.path and i.code != issue.code
        )
        questions.append(
            QuestionIntent(
                question_id=f"q_{issue.code}_{priority}",
                path=issue.path,
                code=issue.code,
                audience=(
                    QuestionAudience.OWNER
                    if issue.code in _OWNER_CODES
                    else QuestionAudience.OPERATOR
                ),
                semantic_intent=_INTENT_MAP.get(issue.code, f"resolve_{issue.code}"),
                related_issue_codes=related,
                priority=priority,
            )
        )
        priority += 1

    for issue in result.warnings:
        key = (issue.code, issue.path)
        if key in seen:
            continue
        seen.add(key)
        if len(questions) >= MAX_QUESTIONS:
            omitted += 1
            continue
        questions.append(
            QuestionIntent(
                question_id=f"q_{issue.code}_{priority}",
                path=issue.path,
                code=issue.code,
                audience=QuestionAudience.OPERATOR,
                semantic_intent=_INTENT_MAP.get(issue.code, f"review_{issue.code}"),
                related_issue_codes=(),
                priority=priority,
            )
        )
        priority += 1

    blocker_q = sum(
        1
        for q in questions
        if any(
            i.code == q.code and i.path == q.path and i.severity is IssueSeverity.BLOCKER
            for i in result.issues
        )
    )
    warning_q = len(questions) - blocker_q

    return QuestionPlan(
        questions=tuple(questions),
        blocker_question_count=blocker_q,
        warning_question_count=warning_q,
        draft_digest=result.draft_digest,
        omitted_count=omitted,
    )
