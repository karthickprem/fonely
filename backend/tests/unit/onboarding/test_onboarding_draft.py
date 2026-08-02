"""Comprehensive deterministic tests for onboarding Stage A."""

from datetime import time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fonely.domain.onboarding.enums import (
    ActivationDecision,
    BusinessCategory,
    DraftStatus,
    PriceKind,
    QuestionAudience,
    ResourceType,
    ReviewStatus,
    SourceType,
    Weekday,
)
from fonely.domain.onboarding.errors import (
    DraftLimitExceededError,
    StaleApprovalError,
    UnresolvedBlockersError,
)
from fonely.domain.onboarding.limits import (
    MAX_LOCATIONS,
    MAX_SERVICES,
    MAX_SHORT_TEXT,
    SCHEMA_VERSION,
)
from fonely.domain.onboarding.models import (
    AddressComponents,
    BusinessOnboardingDraft,
    ContactInfo,
    LocationDraft,
    PolicyDraft,
    PricePolicy,
    ProductDraft,
    ProvenanceField,
    ResourceDraft,
    ScheduleException,
    SchedulePeriod,
    ServiceDraft,
    SourceReference,
    WeeklySchedule,
)
from fonely.domain.onboarding.questions import plan_questions
from fonely.domain.onboarding.review import (
    approve_draft,
    check_activation_readiness,
    create_review_proposal,
)
from fonely.domain.onboarding.validation import validate_draft


def _source(source_type: SourceType = SourceType.OPERATOR_ENTRY) -> SourceReference:
    return SourceReference(source_type=source_type, source_id="src-1", owner_provided=True)


def _provenance(
    status: ReviewStatus = ReviewStatus.CLEAR,
    sources: tuple[SourceReference, ...] | None = None,
) -> ProvenanceField:
    return ProvenanceField(
        review_status=status,
        sources=sources or (_source(),),
    )


def _price(
    kind: PriceKind = PriceKind.FIXED,
    amount: Decimal | None = Decimal("500.00"),
    currency: str = "INR",
    **kwargs: object,
) -> PricePolicy:
    return PricePolicy(kind=kind, currency=currency, amount=amount, **kwargs)


def _schedule() -> WeeklySchedule:
    return WeeklySchedule(
        periods=(
            SchedulePeriod(day=Weekday.MONDAY, start=time(9, 0), end=time(18, 0)),
            SchedulePeriod(day=Weekday.TUESDAY, start=time(9, 0), end=time(18, 0)),
            SchedulePeriod(day=Weekday.WEDNESDAY, start=time(9, 0), end=time(18, 0)),
            SchedulePeriod(day=Weekday.THURSDAY, start=time(9, 0), end=time(18, 0)),
            SchedulePeriod(day=Weekday.FRIDAY, start=time(9, 0), end=time(18, 0)),
            SchedulePeriod(day=Weekday.SATURDAY, start=time(9, 0), end=time(14, 0)),
            SchedulePeriod(day=Weekday.SUNDAY, is_closed=True, start=time(0, 0), end=time(0, 0)),
        )
    )


def _location(key: str = "loc-1") -> LocationDraft:
    return LocationDraft(
        key=key,
        display_name="Lotus Salon - Anna Nagar",
        address=AddressComponents(line1="123 Main St", city="Chennai", state="Tamil Nadu"),
        contact=ContactInfo(phone="+919123456789"),
        schedule=_schedule(),
        provenance=_provenance(),
    )


def _service(
    key: str = "svc-haircut",
    resource_keys: tuple[str, ...] = ("res-anitha",),
) -> ServiceDraft:
    return ServiceDraft(
        key=key,
        name="Haircut",
        location_keys=("loc-1",),
        duration_minutes=30,
        category="hair",
        price=_price(),
        eligible_resource_keys=resource_keys,
        provenance=_provenance(),
    )


def _resource(key: str = "res-anitha") -> ResourceDraft:
    return ResourceDraft(
        key=key,
        display_name="Anitha",
        resource_type=ResourceType.STAFF,
        location_keys=("loc-1",),
        service_keys=("svc-haircut",),
        schedule=_schedule(),
        provenance=_provenance(),
    )


def _product(key: str = "prod-shampoo") -> ProductDraft:
    return ProductDraft(
        key=key,
        name="Shampoo Bottle",
        unit="bottle",
        category="haircare",
        price=_price(),
        location_keys=("loc-1",),
        provenance=_provenance(),
    )


def _minimal_salon() -> BusinessOnboardingDraft:
    return BusinessOnboardingDraft(
        draft_id="draft-salon-1",
        business_name="Lotus Salon",
        business_category=BusinessCategory.SALON,
        default_timezone="Asia/Kolkata",
        default_currency="INR",
        preferred_languages=("ta-IN", "en-IN"),
        locations=(_location(),),
        services=(_service(),),
        resources=(_resource(),),
        business_name_provenance=_provenance(),
        business_category_provenance=_provenance(),
        timezone_provenance=_provenance(),
        currency_provenance=_provenance(),
    )


def _complete_salon() -> BusinessOnboardingDraft:
    return _minimal_salon().with_updates(
        contact=ContactInfo(phone="+919123456789", email="lotus@example.com"),
        policy=PolicyDraft(
            advance_booking_days=30,
            minimum_notice_minutes=60,
            cancellation_cutoff_minutes=120,
            rescheduling_allowed=True,
            walk_in_allowed=True,
            resource_selection_required=False,
        ),
        products=(_product(),),
    )


# ============================================================
# A. Construction and strictness
# ============================================================


class TestConstruction:
    def test_minimal_salon_draft_is_valid(self) -> None:
        draft = _minimal_salon()
        assert draft.schema_version == SCHEMA_VERSION
        assert draft.business_name == "Lotus Salon"
        assert draft.status is DraftStatus.INTAKE

    def test_complete_salon_draft_is_valid(self) -> None:
        draft = _complete_salon()
        assert len(draft.products) == 1
        assert draft.policy.advance_booking_days == 30

    def test_product_business_draft(self) -> None:
        draft = BusinessOnboardingDraft(
            draft_id="draft-shop-1",
            business_name="Corner Store",
            business_category=BusinessCategory.SHOP,
            default_timezone="Asia/Kolkata",
            default_currency="INR",
            locations=(_location(),),
            products=(_product(),),
            business_name_provenance=_provenance(),
        )
        result = validate_draft(draft)
        assert result.blocker_count == 0

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BusinessOnboardingDraft(
                draft_id="d1",
                invented_field="bad",  # type: ignore[call-arg]
            )

    def test_collection_limits_enforced(self) -> None:
        locations = tuple(_location(key=f"loc-{i}") for i in range(MAX_LOCATIONS + 1))
        with pytest.raises(DraftLimitExceededError, match="locations"):
            BusinessOnboardingDraft(
                draft_id="d1",
                default_timezone="Asia/Kolkata",
                locations=locations,
            )

    def test_string_limit(self) -> None:
        with pytest.raises(ValidationError):
            BusinessOnboardingDraft(
                draft_id="x" * (MAX_SHORT_TEXT + 1),
            )

    def test_frozen_model(self) -> None:
        draft = _minimal_salon()
        with pytest.raises(ValidationError):
            draft.business_name = "Changed"  # type: ignore[misc]

    def test_with_updates_returns_new_instance(self) -> None:
        draft = _minimal_salon()
        updated = draft.with_updates(business_name="New Name")
        assert updated.business_name == "New Name"
        assert draft.business_name == "Lotus Salon"


# ============================================================
# B. Pricing
# ============================================================


class TestPricing:
    def test_fixed_price(self) -> None:
        p = _price(PriceKind.FIXED, Decimal("500.00"))
        assert p.amount == Decimal("500.00")

    def test_starting_from_price(self) -> None:
        p = PricePolicy(kind=PriceKind.STARTING_FROM, currency="INR", minimum=Decimal("200"))
        assert p.minimum == Decimal("200")

    def test_range_price(self) -> None:
        p = PricePolicy(
            kind=PriceKind.RANGE,
            currency="INR",
            minimum=Decimal("200"),
            maximum=Decimal("800"),
        )
        assert p.minimum < p.maximum  # type: ignore[operator]

    def test_variable_price(self) -> None:
        p = PricePolicy(kind=PriceKind.VARIABLE, currency="INR")
        assert p.amount is None

    def test_consultation_required_price(self) -> None:
        p = PricePolicy(kind=PriceKind.CONSULTATION_REQUIRED, currency="INR")
        assert p.amount is None

    def test_not_provided_price(self) -> None:
        p = PricePolicy(kind=PriceKind.NOT_PROVIDED, currency="INR")
        assert p.amount is None

    def test_fixed_missing_amount(self) -> None:
        with pytest.raises(ValidationError, match="requires amount"):
            PricePolicy(kind=PriceKind.FIXED, currency="INR")

    def test_range_inversion(self) -> None:
        with pytest.raises(ValidationError, match="must not exceed"):
            PricePolicy(
                kind=PriceKind.RANGE,
                currency="INR",
                minimum=Decimal("800"),
                maximum=Decimal("200"),
            )

    def test_variable_with_amount_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not specify"):
            PricePolicy(
                kind=PriceKind.VARIABLE,
                currency="INR",
                amount=Decimal("100"),
            )

    def test_fixed_zero_amount_allowed(self) -> None:
        p = _price(PriceKind.FIXED, Decimal("0.00"))
        assert p.amount == Decimal("0.00")

    def test_fixed_negative_amount_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-negative"):
            _price(PriceKind.FIXED, Decimal("-1.00"))

    def test_range_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-negative"):
            PricePolicy(
                kind=PriceKind.RANGE,
                currency="INR",
                minimum=Decimal("-1"),
                maximum=Decimal("100"),
            )

    def test_currency_mismatch_detected(self) -> None:
        draft = _minimal_salon().with_updates(
            services=(_service().model_copy(update={"price": _price(currency="USD")}),)
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.issues}
        assert "currency_mismatch" in codes


# ============================================================
# C. Provenance
# ============================================================


class TestProvenance:
    def test_single_source(self) -> None:
        p = _provenance()
        assert len(p.sources) == 1
        assert p.review_status is ReviewStatus.CLEAR

    def test_multiple_sources(self) -> None:
        p = ProvenanceField(
            review_status=ReviewStatus.CLEAR,
            sources=(_source(SourceType.OPERATOR_ENTRY), _source(SourceType.IMAGE)),
        )
        assert len(p.sources) == 2

    def test_conflicting_status(self) -> None:
        p = _provenance(ReviewStatus.CONFLICTING)
        assert p.review_status is ReviewStatus.CONFLICTING

    def test_correction_preserves_original(self) -> None:
        original = _provenance(ReviewStatus.AMBIGUOUS)
        corrected = original.with_correction("Lotus Salon", _source(SourceType.OWNER_FORM))
        assert corrected.review_status is ReviewStatus.OWNER_CORRECTED
        assert corrected.resolved_value == "Lotus Salon"
        assert len(corrected.sources) == 2
        assert corrected.sources[0] == original.sources[0]

    def test_confirmation(self) -> None:
        original = _provenance(ReviewStatus.CLEAR)
        confirmed = original.with_confirmation(_source(SourceType.OWNER_FORM))
        assert confirmed.review_status is ReviewStatus.OWNER_CONFIRMED

    def test_unreadable_creates_blocker(self) -> None:
        draft = _minimal_salon().with_updates(
            business_name_provenance=_provenance(ReviewStatus.UNREADABLE)
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "unreadable_business_name" in codes

    def test_unsupported_creates_blocker(self) -> None:
        draft = _minimal_salon().with_updates(
            business_name_provenance=_provenance(ReviewStatus.UNSUPPORTED)
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "unsupported_business_name" in codes


# ============================================================
# D. Schedule
# ============================================================


class TestSchedule:
    def test_valid_weekly_schedule(self) -> None:
        s = _schedule()
        assert len(s.periods) == 7

    def test_overlap_detected(self) -> None:
        draft = _minimal_salon().with_updates(
            locations=(
                _location().model_copy(
                    update={
                        "schedule": WeeklySchedule(
                            periods=(
                                SchedulePeriod(
                                    day=Weekday.MONDAY, start=time(9, 0), end=time(14, 0)
                                ),
                                SchedulePeriod(
                                    day=Weekday.MONDAY, start=time(13, 0), end=time(18, 0)
                                ),
                            )
                        )
                    }
                ),
            )
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.issues}
        assert "schedule_overlap" in codes

    def test_closed_day(self) -> None:
        p = SchedulePeriod(day=Weekday.SUNDAY, is_closed=True, start=time(0, 0), end=time(0, 0))
        assert p.is_closed

    def test_exception(self) -> None:
        e = ScheduleException(date="2026-01-26", is_closed=True, reason="Republic Day")
        assert e.is_closed

    def test_open_exception_requires_times(self) -> None:
        with pytest.raises(ValidationError, match="requires start and end"):
            ScheduleException(date="2026-01-26", is_closed=False)

    def test_invalid_interval_rejected(self) -> None:
        with pytest.raises(ValidationError, match="start must be before end"):
            SchedulePeriod(day=Weekday.MONDAY, start=time(18, 0), end=time(9, 0))

    def test_buffer_limits(self) -> None:
        s = ServiceDraft(
            key="s1",
            name="Test",
            duration_minutes=30,
            buffer_before_minutes=120,
            buffer_after_minutes=120,
            price=_price(),
            eligible_resource_keys=("r1",),
        )
        assert s.buffer_before_minutes == 120
        with pytest.raises(ValidationError):
            ServiceDraft(
                key="s2",
                name="Test",
                duration_minutes=30,
                buffer_before_minutes=121,
                price=_price(),
            )


# ============================================================
# E. Cross references
# ============================================================


class TestCrossReferences:
    def test_missing_location_ref(self) -> None:
        draft = _minimal_salon().with_updates(
            services=(_service().model_copy(update={"location_keys": ("nonexistent",)}),)
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "invalid_service_location_ref" in codes

    def test_missing_resource_ref(self) -> None:
        draft = _minimal_salon().with_updates(services=(_service(resource_keys=("nonexistent",)),))
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "invalid_service_resource_ref" in codes

    def test_active_service_without_resource(self) -> None:
        draft = _minimal_salon().with_updates(services=(_service(resource_keys=()),))
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "service_no_eligible_resource" in codes

    def test_resource_free_service_allowed(self) -> None:
        draft = _minimal_salon().with_updates(
            services=(_service(resource_keys=()).model_copy(update={"requires_resource": False}),)
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "service_no_eligible_resource" not in codes

    def test_duplicate_service_key(self) -> None:
        draft = _minimal_salon().with_updates(
            services=(
                _service(),
                _service(),
            )
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "duplicate_service_key" in codes

    def test_duplicate_service_name_warning(self) -> None:
        draft = _minimal_salon().with_updates(
            services=(
                _service(key="svc-1"),
                _service(key="svc-2"),
            )
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.warnings}
        assert "duplicate_service_name" in codes

    def test_resource_invalid_location_ref(self) -> None:
        draft = _minimal_salon().with_updates(
            resources=(_resource().model_copy(update={"location_keys": ("nonexistent",)}),)
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "invalid_resource_location_ref" in codes

    def test_resource_invalid_service_ref(self) -> None:
        draft = _minimal_salon().with_updates(
            resources=(_resource().model_copy(update={"service_keys": ("nonexistent",)}),)
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "invalid_resource_service_ref" in codes

    def test_product_invalid_location_ref(self) -> None:
        draft = _minimal_salon().with_updates(
            products=(_product().model_copy(update={"location_keys": ("nonexistent",)}),)
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "invalid_product_location_ref" in codes


# ============================================================
# F. Validation result
# ============================================================


class TestValidationResult:
    def test_valid_salon_has_no_blockers(self) -> None:
        result = validate_draft(_minimal_salon())
        assert result.blocker_count == 0

    def test_deterministic_issue_ordering(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result1 = validate_draft(draft)
        result2 = validate_draft(draft)
        assert result1.issues == result2.issues

    def test_stable_issue_codes(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result = validate_draft(draft)
        assert all(isinstance(i.code, str) and len(i.code) > 0 for i in result.issues)

    def test_incomplete_draft_typed_issues(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result = validate_draft(draft)
        assert result.blocker_count > 0
        codes = {i.code for i in result.blockers}
        assert "missing_business_name" in codes
        assert "missing_timezone" in codes
        assert "no_active_location" in codes

    def test_missing_service_duration(self) -> None:
        svc = _service().model_copy(update={"duration_minutes": None})
        draft = _minimal_salon().with_updates(services=(svc,))
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "missing_service_duration" in codes

    def test_missing_service_price(self) -> None:
        svc = _service().model_copy(update={"price": None})
        draft = _minimal_salon().with_updates(services=(svc,))
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "missing_service_price" in codes

    def test_not_provided_price_is_blocker(self) -> None:
        svc = _service().model_copy(
            update={"price": PricePolicy(kind=PriceKind.NOT_PROVIDED, currency="INR")}
        )
        draft = _minimal_salon().with_updates(services=(svc,))
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "price_not_provided" in codes

    def test_unsupported_schema_is_blocker(self) -> None:
        draft = _minimal_salon().with_updates(schema_version=999)
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "unsupported_schema" in codes


# ============================================================
# G. Question plan
# ============================================================


class TestQuestionPlan:
    def test_blocker_prioritization(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result = validate_draft(draft)
        plan = plan_questions(result)
        assert plan.blocker_question_count > 0
        assert plan.questions[0].priority == 0

    def test_deduplication(self) -> None:
        draft = _minimal_salon().with_updates(
            services=(
                _service(key="svc-1", resource_keys=("nonexistent",)),
                _service(key="svc-2", resource_keys=("nonexistent2",)),
            )
        )
        result = validate_draft(draft)
        plan = plan_questions(result)
        codes = [q.code for q in plan.questions]
        assert codes.count("invalid_service_resource_ref") <= 1

    def test_stable_ordering(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result = validate_draft(draft)
        plan1 = plan_questions(result)
        plan2 = plan_questions(result)
        assert plan1.questions == plan2.questions

    def test_owner_audience(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result = validate_draft(draft)
        plan = plan_questions(result)
        owner_q = [q for q in plan.questions if q.audience is QuestionAudience.OWNER]
        assert len(owner_q) > 0

    def test_digest_matches(self) -> None:
        draft = _minimal_salon()
        result = validate_draft(draft)
        plan = plan_questions(result)
        assert plan.draft_digest == result.draft_digest


# ============================================================
# H. Review and approval
# ============================================================


class TestReviewAndApproval:
    def test_exact_digest_approval(self) -> None:
        draft = _minimal_salon()
        digest = draft.canonical_digest()
        result = approve_draft(draft, reviewer_ref="owner-1", expected_digest=digest)
        assert result.approved
        assert result.draft_digest == digest

    def test_stale_digest_rejected(self) -> None:
        draft = _minimal_salon()
        with pytest.raises(StaleApprovalError):
            approve_draft(draft, reviewer_ref="owner-1", expected_digest="stale-digest")

    def test_blockers_reject_approval(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        with pytest.raises(UnresolvedBlockersError):
            approve_draft(
                draft,
                reviewer_ref="owner-1",
                expected_digest=draft.canonical_digest(),
            )

    def test_correction_changes_digest(self) -> None:
        draft = _minimal_salon()
        d1 = draft.canonical_digest()
        updated = draft.with_updates(business_name="New Lotus Salon")
        d2 = updated.canonical_digest()
        assert d1 != d2

    def test_idempotent_approval(self) -> None:
        draft = _minimal_salon()
        digest = draft.canonical_digest()
        r1 = approve_draft(draft, reviewer_ref="owner-1", expected_digest=digest)
        r2 = approve_draft(draft, reviewer_ref="owner-1", expected_digest=digest)
        assert r1.draft_digest == r2.draft_digest

    def test_review_proposal(self) -> None:
        draft = _minimal_salon()
        proposal = create_review_proposal(draft)
        assert proposal.can_approve
        assert proposal.blocker_count == 0


# ============================================================
# I. Activation readiness
# ============================================================


class TestActivationReadiness:
    def test_ready_salon(self) -> None:
        draft = _minimal_salon()
        digest = draft.canonical_digest()
        result = check_activation_readiness(draft, approved_digest=digest, reviewer_ref="owner-1")
        assert result.decision is ActivationDecision.REQUIRES_TEST_MODE

    def test_not_ready_no_approval(self) -> None:
        draft = _minimal_salon()
        result = check_activation_readiness(draft, approved_digest=None, reviewer_ref=None)
        assert result.decision is ActivationDecision.NOT_READY

    def test_not_ready_stale_approval(self) -> None:
        draft = _minimal_salon()
        result = check_activation_readiness(draft, approved_digest="stale", reviewer_ref="owner-1")
        assert result.decision is ActivationDecision.NOT_READY
        assert any("re-approval" in r for r in result.reasons)

    def test_not_ready_missing_location(self) -> None:
        draft = _minimal_salon().with_updates(locations=())
        digest = draft.canonical_digest()
        result = check_activation_readiness(draft, approved_digest=digest, reviewer_ref="owner-1")
        assert result.decision is ActivationDecision.NOT_READY

    def test_not_ready_unresolved_price(self) -> None:
        svc = _service().model_copy(
            update={"price": PricePolicy(kind=PriceKind.NOT_PROVIDED, currency="INR")}
        )
        draft = _minimal_salon().with_updates(services=(svc,))
        digest = draft.canonical_digest()
        result = check_activation_readiness(draft, approved_digest=digest, reviewer_ref="owner-1")
        assert result.decision is ActivationDecision.NOT_READY

    def test_blocked_unsupported_schema(self) -> None:
        draft = _minimal_salon().with_updates(schema_version=999)
        result = check_activation_readiness(
            draft, approved_digest=draft.canonical_digest(), reviewer_ref="owner-1"
        )
        assert result.decision is ActivationDecision.BLOCKED_UNSUPPORTED

    def test_no_false_ready(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result = check_activation_readiness(
            draft, approved_digest=draft.canonical_digest(), reviewer_ref="owner-1"
        )
        assert result.decision is not ActivationDecision.READY


# ============================================================
# J. Canonical serialization and digest
# ============================================================


class TestDigest:
    def test_stable_across_construction_order(self) -> None:
        d1 = _minimal_salon()
        d2 = BusinessOnboardingDraft(
            draft_id="draft-salon-1",
            default_currency="INR",
            business_name="Lotus Salon",
            business_category=BusinessCategory.SALON,
            default_timezone="Asia/Kolkata",
            preferred_languages=("ta-IN", "en-IN"),
            resources=(_resource(),),
            locations=(_location(),),
            services=(_service(),),
            business_name_provenance=_provenance(),
            business_category_provenance=_provenance(),
            timezone_provenance=_provenance(),
            currency_provenance=_provenance(),
        )
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_changed_service_changes_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(services=(_service().model_copy(update={"name": "Premium Haircut"}),))
        assert d1.canonical_digest() != d2.canonical_digest()

    def test_changed_price_changes_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(
            services=(_service().model_copy(update={"price": _price(amount=Decimal("600.00"))}),)
        )
        assert d1.canonical_digest() != d2.canonical_digest()

    def test_changed_schedule_changes_digest(self) -> None:
        d1 = _minimal_salon()
        new_loc = _location().model_copy(update={"schedule": WeeklySchedule(periods=())})
        d2 = d1.with_updates(locations=(new_loc,))
        assert d1.canonical_digest() != d2.canonical_digest()

    def test_excluded_metadata_does_not_change_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(created_at="2026-01-01", updated_at="2026-08-02")
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_status_does_not_change_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(status=DraftStatus.APPROVED)
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_source_batches_do_not_change_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(source_batches=("batch-1",))
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_changed_resource_changes_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(resources=(_resource().model_copy(update={"display_name": "Priya"}),))
        assert d1.canonical_digest() != d2.canonical_digest()

    def test_changed_eligibility_changes_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(services=(_service(resource_keys=("res-anitha", "res-priya")),))
        assert d1.canonical_digest() != d2.canonical_digest()

    def test_changed_policy_changes_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(policy=PolicyDraft(advance_booking_days=60))
        assert d1.canonical_digest() != d2.canonical_digest()


# ============================================================
# K. Performance sanity
# ============================================================


class TestPerformanceSanity:
    def test_max_sized_draft_validates_bounded(self) -> None:
        locations = tuple(_location(key=f"loc-{i}") for i in range(MAX_LOCATIONS))
        services = tuple(
            ServiceDraft(
                key=f"svc-{i}",
                name=f"Service {i}",
                location_keys=(f"loc-{i % MAX_LOCATIONS}",),
                duration_minutes=30,
                price=_price(),
                eligible_resource_keys=(f"res-{i % 10}",),
            )
            for i in range(MAX_SERVICES)
        )
        resources = tuple(
            ResourceDraft(
                key=f"res-{i}",
                display_name=f"Staff {i}",
                location_keys=(f"loc-{i % MAX_LOCATIONS}",),
                service_keys=tuple(f"svc-{j}" for j in range(i, min(i + 5, MAX_SERVICES))),
            )
            for i in range(50)
        )
        draft = BusinessOnboardingDraft(
            draft_id="big-draft",
            business_name="Big Salon",
            default_timezone="Asia/Kolkata",
            locations=locations,
            services=services,
            resources=resources,
            business_name_provenance=_provenance(),
        )
        result = validate_draft(draft)
        assert len(result.issues) <= 500
        plan = plan_questions(result)
        assert len(plan.questions) <= 200


# ============================================================
# Policy validation
# ============================================================


class TestPolicyValidation:
    def test_incoherent_cancellation_notice(self) -> None:
        draft = _minimal_salon().with_updates(
            policy=PolicyDraft(
                minimum_notice_minutes=120,
                cancellation_cutoff_minutes=60,
            )
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.warnings}
        assert "incoherent_cancellation_notice" in codes
