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
        "unresolved_business_name",
        "currency_mismatch",
        "service_no_eligible_resource",
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
    "service_no_eligible_resource": "assign_resource_to_service",
    "missing_service_duration": "provide_service_duration",
    "missing_service_price": "provide_service_price",
    "price_not_provided": "classify_and_provide_price",
    "currency_mismatch": "correct_price_currency",
    "schedule_overlap": "resolve_schedule_overlap",
    "unresolved_business_name": "resolve_business_name_conflict",
    "unreadable_business_name": "provide_business_name_manually",
    "unsupported_business_name": "provide_business_name_manually",
    "unsupported_schema": "upgrade_draft_schema",
    "invalid_location_timezone": "correct_location_timezone",
    "invalid_currency": "correct_currency_code",
    "incoherent_cancellation_notice": "review_cancellation_policy",
}


def plan_questions(result: ValidationResult) -> QuestionPlan:
    seen_codes: set[str] = set()
    questions: list[QuestionIntent] = []
    priority = 0

    for issue in result.blockers:
        if issue.code in seen_codes:
            continue
        seen_codes.add(issue.code)
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
        if issue.code in seen_codes:
            continue
        seen_codes.add(issue.code)
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

    if len(questions) > MAX_QUESTIONS:
        questions = questions[:MAX_QUESTIONS]

    blocker_q = sum(
        1
        for q in questions
        if any(i.code == q.code and i.severity is IssueSeverity.BLOCKER for i in result.issues)
    )
    warning_q = len(questions) - blocker_q

    return QuestionPlan(
        questions=tuple(questions),
        blocker_question_count=blocker_q,
        warning_question_count=warning_q,
        draft_digest=result.draft_digest,
    )
