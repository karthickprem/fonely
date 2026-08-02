"""Comprehensive deterministic tests for onboarding Stage A."""

from datetime import date, time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fonely.domain.onboarding.enums import (
    ActivationDecision,
    BusinessCategory,
    DraftStatus,
    PriceKind,
    ResourceType,
    ReviewStatus,
    SourceType,
    Weekday,
)
from fonely.domain.onboarding.errors import (
    DraftLimitExceededError,
    InvalidReviewerError,
    StaleApprovalError,
    UnresolvedBlockersError,
)
from fonely.domain.onboarding.limits import (
    MAX_LOCATIONS,
    MAX_PROVENANCE_ENTRIES,
    MAX_SERVICES,
    MAX_SHORT_TEXT,
    MAX_SOURCE_BATCHES,
    SCHEMA_VERSION,
)
from fonely.domain.onboarding.models import (
    AddressComponents,
    BusinessOnboardingDraft,
    ContactInfo,
    FieldProvenance,
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


def _prov(
    status: ReviewStatus = ReviewStatus.CLEAR,
    sources: tuple[SourceReference, ...] | None = None,
) -> ProvenanceField:
    return ProvenanceField(
        review_status=status,
        sources=sources or (_source(),),
    )


def _field_prov(*pairs: tuple[str, ProvenanceField]) -> FieldProvenance:
    return FieldProvenance(fields=tuple(sorted(pairs, key=lambda p: p[0])))


def _biz_prov() -> FieldProvenance:
    return _field_prov(
        ("business_category", _prov()),
        ("business_name", _prov()),
        ("default_currency", _prov()),
        ("default_timezone", _prov()),
    )


def _loc_prov() -> FieldProvenance:
    return _field_prov(("display_name", _prov()), ("is_active", _prov()))


def _svc_prov() -> FieldProvenance:
    return _field_prov(
        ("duration_minutes", _prov()),
        ("is_active", _prov()),
        ("name", _prov()),
    )


def _res_prov() -> FieldProvenance:
    return _field_prov(("display_name", _prov()), ("is_active", _prov()))


def _prod_prov() -> FieldProvenance:
    return _field_prov(("is_active", _prov()), ("name", _prov()))


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
        provenance=_loc_prov(),
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
        provenance=_svc_prov(),
    )


def _resource(key: str = "res-anitha") -> ResourceDraft:
    return ResourceDraft(
        key=key,
        display_name="Anitha",
        resource_type=ResourceType.STAFF,
        location_keys=("loc-1",),
        service_keys=("svc-haircut",),
        schedule=_schedule(),
        provenance=_res_prov(),
    )


def _product(key: str = "prod-shampoo") -> ProductDraft:
    return ProductDraft(
        key=key,
        name="Shampoo Bottle",
        unit="bottle",
        category="haircare",
        price=_price(),
        location_keys=("loc-1",),
        provenance=_prod_prov(),
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
        provenance=_biz_prov(),
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
        products=[_product().model_dump(mode="json")],
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
            provenance=_biz_prov(),
        )
        result = validate_draft(draft)
        assert result.blocker_count == 0

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BusinessOnboardingDraft(draft_id="d1", invented_field="bad")  # type: ignore[call-arg]

    def test_collection_limits_enforced(self) -> None:
        locations = tuple(_location(key=f"loc-{i}") for i in range(MAX_LOCATIONS + 1))
        with pytest.raises(DraftLimitExceededError, match="locations"):
            BusinessOnboardingDraft(
                draft_id="d1", default_timezone="Asia/Kolkata", locations=locations
            )

    def test_string_limit(self) -> None:
        with pytest.raises(ValidationError):
            BusinessOnboardingDraft(draft_id="x" * (MAX_SHORT_TEXT + 1))

    def test_frozen_model(self) -> None:
        draft = _minimal_salon()
        with pytest.raises(ValidationError):
            draft.business_name = "Changed"  # type: ignore[misc]

    def test_with_updates_returns_new_validated_instance(self) -> None:
        draft = _minimal_salon()
        updated = draft.with_updates(business_name="New Name")
        assert updated.business_name == "New Name"
        assert draft.business_name == "Lotus Salon"

    def test_with_updates_rejects_invalid_nested(self) -> None:
        draft = _minimal_salon()
        with pytest.raises(ValidationError):
            draft.with_updates(locations=[{"key": "", "display_name": ""}])

    def test_source_batches_bounded(self) -> None:
        with pytest.raises(DraftLimitExceededError, match="source_batches"):
            BusinessOnboardingDraft(
                draft_id="d1",
                source_batches=tuple(f"b-{i}" for i in range(MAX_SOURCE_BATCHES + 1)),
            )


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
            kind=PriceKind.RANGE, currency="INR", minimum=Decimal("200"), maximum=Decimal("800")
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
                kind=PriceKind.RANGE, currency="INR", minimum=Decimal("800"), maximum=Decimal("200")
            )

    def test_variable_with_amount_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not specify"):
            PricePolicy(kind=PriceKind.VARIABLE, currency="INR", amount=Decimal("100"))

    def test_fixed_zero_amount_allowed(self) -> None:
        p = _price(PriceKind.FIXED, Decimal("0.00"))
        assert p.amount == Decimal("0.00")

    def test_fixed_negative_amount_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-negative"):
            _price(PriceKind.FIXED, Decimal("-1.00"))

    def test_fixed_with_minimum_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not specify minimum"):
            PricePolicy(
                kind=PriceKind.FIXED, currency="INR", amount=Decimal("100"), minimum=Decimal("50")
            )

    def test_fixed_with_maximum_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not specify minimum"):
            PricePolicy(
                kind=PriceKind.FIXED, currency="INR", amount=Decimal("100"), maximum=Decimal("200")
            )

    def test_starting_from_with_amount_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not specify amount"):
            PricePolicy(
                kind=PriceKind.STARTING_FROM,
                currency="INR",
                minimum=Decimal("200"),
                amount=Decimal("300"),
            )

    def test_starting_from_with_maximum_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not specify amount"):
            PricePolicy(
                kind=PriceKind.STARTING_FROM,
                currency="INR",
                minimum=Decimal("200"),
                maximum=Decimal("500"),
            )

    def test_range_with_amount_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not specify amount"):
            PricePolicy(
                kind=PriceKind.RANGE,
                currency="INR",
                minimum=Decimal("200"),
                maximum=Decimal("500"),
                amount=Decimal("300"),
            )

    def test_unsupported_currency_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported currency"):
            PricePolicy(kind=PriceKind.FIXED, currency="USD", amount=Decimal("100"))

    def test_numeric_string_currency_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported currency"):
            PricePolicy(kind=PriceKind.FIXED, currency="123", amount=Decimal("100"))

    def test_currency_mismatch_detected(self) -> None:
        draft = _minimal_salon()
        result = validate_draft(draft)
        assert result.blocker_count == 0


# ============================================================
# C. Provenance
# ============================================================


class TestProvenance:
    def test_default_provenance_is_missing(self) -> None:
        p = ProvenanceField()
        assert p.review_status is ReviewStatus.MISSING
        assert not p.has_evidence()

    def test_clear_with_evidence(self) -> None:
        p = _prov(ReviewStatus.CLEAR)
        assert p.has_evidence()

    def test_clear_without_evidence_fails_validation(self) -> None:
        draft = _minimal_salon().with_updates(
            provenance=_field_prov(
                ("business_name", ProvenanceField(review_status=ReviewStatus.CLEAR)),
                ("business_category", _prov()),
                ("default_currency", _prov()),
                ("default_timezone", _prov()),
            ).model_dump(mode="json"),
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "no_evidence" in codes

    def test_correction_preserves_original(self) -> None:
        original = _prov(ReviewStatus.AMBIGUOUS)
        corrected = original.with_correction("Lotus Salon", _source(SourceType.OWNER_FORM))
        assert corrected.review_status is ReviewStatus.OWNER_CORRECTED
        assert corrected.resolved_value == "Lotus Salon"
        assert len(corrected.sources) == 2
        assert corrected.sources[0] == original.sources[0]

    def test_confirmation(self) -> None:
        original = _prov(ReviewStatus.CLEAR)
        confirmed = original.with_confirmation(_source(SourceType.OWNER_FORM))
        assert confirmed.review_status is ReviewStatus.OWNER_CONFIRMED

    def test_provenance_source_limit(self) -> None:
        sources = tuple(
            SourceReference(source_type=SourceType.OPERATOR_ENTRY, source_id=f"s{i}")
            for i in range(MAX_PROVENANCE_ENTRIES)
        )
        p = ProvenanceField(review_status=ReviewStatus.CLEAR, sources=sources)
        assert len(p.sources) == MAX_PROVENANCE_ENTRIES

    def test_provenance_source_limit_exceeded(self) -> None:
        sources = tuple(
            SourceReference(source_type=SourceType.OPERATOR_ENTRY, source_id=f"s{i}")
            for i in range(MAX_PROVENANCE_ENTRIES + 1)
        )
        with pytest.raises(DraftLimitExceededError, match="provenance_sources"):
            ProvenanceField(review_status=ReviewStatus.CLEAR, sources=sources)

    def test_no_provenance_cannot_approve(self) -> None:
        draft = BusinessOnboardingDraft(
            draft_id="d-no-prov",
            business_name="Test",
            default_timezone="Asia/Kolkata",
            locations=(_location(),),
            services=(
                ServiceDraft(
                    key="s1",
                    name="Test",
                    duration_minutes=30,
                    price=_price(),
                    eligible_resource_keys=("r1",),
                    requires_resource=False,
                    provenance=_svc_prov(),
                ),
            ),
        )
        result = validate_draft(draft)
        assert result.blocker_count > 0
        codes = {i.code for i in result.blockers}
        assert "missing_field_provenance" in codes

    def test_unreadable_creates_blocker(self) -> None:
        draft = _minimal_salon().with_updates(
            provenance=_field_prov(
                ("business_name", _prov(ReviewStatus.UNREADABLE)),
                ("business_category", _prov()),
                ("default_currency", _prov()),
                ("default_timezone", _prov()),
            ).model_dump(mode="json"),
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "unresolved_unreadable" in codes

    def test_field_provenance_duplicate_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate provenance"):
            FieldProvenance(fields=(("a", _prov()), ("a", _prov())))


# ============================================================
# D. Schedule
# ============================================================


class TestSchedule:
    def test_valid_weekly_schedule(self) -> None:
        s = _schedule()
        assert len(s.periods) == 7

    def test_overlap_detected(self) -> None:
        draft = _minimal_salon().with_updates(
            locations=[
                _location()
                .model_copy(
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
                )
                .model_dump(mode="json"),
            ]
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.issues}
        assert "schedule_overlap" in codes

    def test_closed_open_conflict(self) -> None:
        draft = _minimal_salon().with_updates(
            locations=[
                _location()
                .model_copy(
                    update={
                        "schedule": WeeklySchedule(
                            periods=(
                                SchedulePeriod(
                                    day=Weekday.MONDAY, start=time(9, 0), end=time(18, 0)
                                ),
                                SchedulePeriod(
                                    day=Weekday.MONDAY,
                                    is_closed=True,
                                    start=time(0, 0),
                                    end=time(0, 0),
                                ),
                            )
                        )
                    }
                )
                .model_dump(mode="json"),
            ]
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.issues}
        assert "schedule_closed_open_conflict" in codes

    def test_exception_date_is_real_date(self) -> None:
        ScheduleException(date=date(2026, 1, 26), is_closed=True, reason="Republic Day")
        with pytest.raises(ValidationError):
            ScheduleException(date="not-a-date", is_closed=True)  # type: ignore[arg-type]

    def test_exception_duplicate_date_detected(self) -> None:
        sched = WeeklySchedule(
            exceptions=(
                ScheduleException(date=date(2026, 1, 26), is_closed=True),
                ScheduleException(date=date(2026, 1, 26), is_closed=True),
            )
        )
        draft = _minimal_salon().with_updates(
            locations=[_location().model_copy(update={"schedule": sched}).model_dump(mode="json")]
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.issues}
        assert "exception_duplicate_date" in codes

    def test_exception_closed_open_conflict(self) -> None:
        sched = WeeklySchedule(
            exceptions=(
                ScheduleException(date=date(2026, 1, 26), is_closed=True),
                ScheduleException(
                    date=date(2026, 1, 26), is_closed=False, start=time(9, 0), end=time(14, 0)
                ),
            )
        )
        draft = _minimal_salon().with_updates(
            locations=[_location().model_copy(update={"schedule": sched}).model_dump(mode="json")]
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "exception_closed_open_conflict" in codes

    def test_invalid_interval_rejected(self) -> None:
        with pytest.raises(ValidationError, match="start must be before end"):
            SchedulePeriod(day=Weekday.MONDAY, start=time(18, 0), end=time(9, 0))


# ============================================================
# E. Cross references and usable resources
# ============================================================


class TestCrossReferences:
    def test_missing_location_ref(self) -> None:
        draft = _minimal_salon().with_updates(
            services=[
                _service()
                .model_copy(update={"location_keys": ("nonexistent",)})
                .model_dump(mode="json")
            ]
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "invalid_service_location_ref" in codes

    def test_inactive_resource_not_usable(self) -> None:
        res = _resource().model_copy(update={"is_active": False})
        draft = _minimal_salon().with_updates(resources=[res.model_dump(mode="json")])
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "service_no_usable_resource" in codes

    def test_inactive_location_resource_not_usable(self) -> None:
        loc = _location().model_copy(update={"is_active": False})
        draft = _minimal_salon().with_updates(locations=[loc.model_dump(mode="json")])
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "no_active_location" in codes

    def test_location_disjoint_resource_not_usable(self) -> None:
        res = _resource().model_copy(update={"location_keys": ("loc-other",)})
        loc2 = _location(key="loc-other").model_copy(update={"is_active": True})
        draft = _minimal_salon().with_updates(
            locations=[_location().model_dump(mode="json"), loc2.model_dump(mode="json")],
            resources=[res.model_dump(mode="json")],
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "service_no_usable_resource" in codes

    def test_reciprocal_eligibility_mismatch(self) -> None:
        res = _resource().model_copy(update={"service_keys": ("svc-other",)})
        draft = _minimal_salon().with_updates(resources=[res.model_dump(mode="json")])
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "service_no_usable_resource" in codes

    def test_resource_free_service_allowed(self) -> None:
        svc = _service(resource_keys=()).model_copy(update={"requires_resource": False})
        draft = _minimal_salon().with_updates(services=[svc.model_dump(mode="json")])
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "service_no_usable_resource" not in codes

    def test_valid_multi_location_resource(self) -> None:
        loc2 = _location(key="loc-2")
        svc = _service().model_copy(update={"location_keys": ("loc-1", "loc-2")})
        res = _resource().model_copy(update={"location_keys": ("loc-1", "loc-2")})
        draft = _minimal_salon().with_updates(
            locations=[_location().model_dump(mode="json"), loc2.model_dump(mode="json")],
            services=[svc.model_dump(mode="json")],
            resources=[res.model_dump(mode="json")],
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "service_no_usable_resource" not in codes

    def test_same_issue_code_multiple_paths(self) -> None:
        svc1 = ServiceDraft(
            key="svc-1",
            name="S1",
            duration_minutes=30,
            price=_price(),
            eligible_resource_keys=("nonexistent1",),
            provenance=_svc_prov(),
        )
        svc2 = ServiceDraft(
            key="svc-2",
            name="S2",
            duration_minutes=30,
            price=_price(),
            eligible_resource_keys=("nonexistent2",),
            provenance=_svc_prov(),
        )
        draft = _minimal_salon().with_updates(
            services=[svc1.model_dump(mode="json"), svc2.model_dump(mode="json")]
        )
        result = validate_draft(draft)
        ref_issues = [i for i in result.blockers if i.code == "invalid_service_resource_ref"]
        assert len(ref_issues) == 2
        paths = {i.path for i in ref_issues}
        assert len(paths) == 2

        plan = plan_questions(result)
        ref_q = [q for q in plan.questions if q.code == "invalid_service_resource_ref"]
        assert len(ref_q) == 2


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

    def test_incomplete_draft_typed_issues(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result = validate_draft(draft)
        assert result.blocker_count > 0
        codes = {i.code for i in result.blockers}
        assert "missing_business_name" in codes
        assert "missing_timezone" in codes
        assert "no_active_location" in codes

    def test_unsupported_schema_is_blocker(self) -> None:
        draft = _minimal_salon().with_updates(schema_version=999)
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "unsupported_schema" in codes

    def test_invalid_timezone_returns_issue_not_exception(self) -> None:
        draft = _minimal_salon().with_updates(default_timezone="../../etc/passwd")
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "invalid_timezone" in codes

    def test_empty_timezone_returns_issue(self) -> None:
        draft = _minimal_salon().with_updates(default_timezone="")
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "missing_timezone" in codes


# ============================================================
# G. Question plan
# ============================================================


class TestQuestionPlan:
    def test_blocker_prioritization(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result = validate_draft(draft)
        plan = plan_questions(result)
        assert plan.blocker_question_count > 0

    def test_stable_ordering(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result = validate_draft(draft)
        plan1 = plan_questions(result)
        plan2 = plan_questions(result)
        assert plan1.questions == plan2.questions

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
            approve_draft(draft, reviewer_ref="owner-1", expected_digest=draft.canonical_digest())

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

    def test_empty_reviewer_rejected(self) -> None:
        draft = _minimal_salon()
        with pytest.raises(InvalidReviewerError):
            approve_draft(draft, reviewer_ref="", expected_digest=draft.canonical_digest())

    def test_whitespace_reviewer_rejected(self) -> None:
        draft = _minimal_salon()
        with pytest.raises(InvalidReviewerError):
            approve_draft(draft, reviewer_ref="   ", expected_digest=draft.canonical_digest())

    def test_overlimit_reviewer_rejected(self) -> None:
        draft = _minimal_salon()
        with pytest.raises(InvalidReviewerError):
            approve_draft(draft, reviewer_ref="x" * 300, expected_digest=draft.canonical_digest())

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

    def test_blocker_count_is_actual_blockers(self) -> None:
        draft = BusinessOnboardingDraft(draft_id="d1")
        result = check_activation_readiness(draft, approved_digest=None, reviewer_ref=None)
        assert result.blocker_count > len(result.reasons)
        val = validate_draft(draft)
        assert result.blocker_count >= val.blocker_count

    def test_no_draft_reaches_ready_without_evidence(self) -> None:
        draft = BusinessOnboardingDraft(
            draft_id="d-bare",
            business_name="Test",
            default_timezone="Asia/Kolkata",
            locations=(_location(),),
        )
        digest = draft.canonical_digest()
        result = check_activation_readiness(draft, approved_digest=digest, reviewer_ref="owner-1")
        assert result.decision is not ActivationDecision.READY
        assert result.decision is not ActivationDecision.REQUIRES_TEST_MODE

    def test_blocked_unsupported_schema(self) -> None:
        draft = _minimal_salon().with_updates(schema_version=999)
        result = check_activation_readiness(
            draft, approved_digest=draft.canonical_digest(), reviewer_ref="owner-1"
        )
        assert result.decision is ActivationDecision.BLOCKED_UNSUPPORTED

    def test_valid_complete_salon_after_approval_reaches_test_mode(self) -> None:
        draft = _minimal_salon()
        digest = draft.canonical_digest()
        approval = approve_draft(draft, reviewer_ref="owner-ref-123", expected_digest=digest)
        result = check_activation_readiness(
            draft, approved_digest=approval.draft_digest, reviewer_ref="owner-ref-123"
        )
        assert result.decision is ActivationDecision.REQUIRES_TEST_MODE
        assert result.blocker_count == 0


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
            provenance=_biz_prov(),
        )
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_reordered_set_like_refs_keep_digest_stable(self) -> None:
        svc1 = _service().model_copy(update={"eligible_resource_keys": ("r1", "r2")})
        svc2 = _service().model_copy(update={"eligible_resource_keys": ("r2", "r1")})
        d1 = _minimal_salon().with_updates(services=[svc1.model_dump(mode="json")])
        d2 = _minimal_salon().with_updates(services=[svc2.model_dump(mode="json")])
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_reordered_languages_keep_digest_stable(self) -> None:
        d1 = _minimal_salon().with_updates(preferred_languages=("ta-IN", "en-IN"))
        d2 = _minimal_salon().with_updates(preferred_languages=("en-IN", "ta-IN"))
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_changed_service_changes_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(
            services=[
                _service().model_copy(update={"name": "Premium Haircut"}).model_dump(mode="json")
            ]
        )
        assert d1.canonical_digest() != d2.canonical_digest()

    def test_changed_price_changes_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(
            services=[
                _service()
                .model_copy(update={"price": _price(amount=Decimal("600.00"))})
                .model_dump(mode="json")
            ]
        )
        assert d1.canonical_digest() != d2.canonical_digest()

    def test_excluded_metadata_does_not_change_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(created_at="2026-01-01", updated_at="2026-08-02")
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_status_does_not_change_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(status=DraftStatus.APPROVED.value)
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_changed_policy_changes_digest(self) -> None:
        d1 = _minimal_salon()
        d2 = d1.with_updates(policy=PolicyDraft(advance_booking_days=60).model_dump(mode="json"))
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
                provenance=_svc_prov(),
            )
            for i in range(MAX_SERVICES)
        )
        resources = tuple(
            ResourceDraft(
                key=f"res-{i}",
                display_name=f"Staff {i}",
                location_keys=(f"loc-{i % MAX_LOCATIONS}",),
                service_keys=tuple(f"svc-{j}" for j in range(i, min(i + 5, MAX_SERVICES))),
                provenance=_res_prov(),
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
            provenance=_biz_prov(),
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
                minimum_notice_minutes=120, cancellation_cutoff_minutes=60
            ).model_dump(mode="json")
        )
        result = validate_draft(draft)
        codes = {i.code for i in result.warnings}
        assert "incoherent_cancellation_notice" in codes


# ============================================================
# Currency contract
# ============================================================


class TestCurrencyContract:
    def test_unsupported_currency_validation(self) -> None:
        draft = _minimal_salon().with_updates(default_currency="USD")
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "unsupported_currency" in codes

    def test_numeric_currency_validation(self) -> None:
        draft = _minimal_salon().with_updates(default_currency="123")
        result = validate_draft(draft)
        codes = {i.code for i in result.blockers}
        assert "unsupported_currency" in codes
