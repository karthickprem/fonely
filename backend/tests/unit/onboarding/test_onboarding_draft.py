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
    MAX_KEY_LENGTH,
    MAX_LOCATIONS,
    MAX_PROVENANCE_ENTRIES,
    MAX_SERVICES,
    MAX_SOURCE_BATCHES,
)
from fonely.domain.onboarding.models import (
    AddressComponents,
    BusinessOnboardingDraft,
    ContactInfo,
    FieldProvenance,
    LocationDraft,
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
)
from fonely.domain.onboarding.validation import validate_draft


def _src(st: SourceType = SourceType.OPERATOR_ENTRY) -> SourceReference:
    return SourceReference(source_type=st, source_id="src-1", owner_provided=True)


def _prov(
    status: ReviewStatus = ReviewStatus.CLEAR,
    sources: tuple[SourceReference, ...] | None = None,
) -> ProvenanceField:
    return ProvenanceField(review_status=status, sources=sources or (_src(),))


def _fp(*pairs: tuple[str, ProvenanceField]) -> FieldProvenance:
    return FieldProvenance(fields=tuple(sorted(pairs, key=lambda p: p[0])))


def _biz_prov() -> FieldProvenance:
    return _fp(
        ("business_category", _prov()),
        ("business_name", _prov()),
        ("default_currency", _prov()),
        ("default_timezone", _prov()),
    )


def _loc_prov() -> FieldProvenance:
    return _fp(
        ("display_name", _prov()),
        ("is_active", _prov()),
        ("schedule", _prov()),
    )


def _svc_prov() -> FieldProvenance:
    return _fp(
        ("duration_minutes", _prov()),
        ("eligible_resource_keys", _prov()),
        ("is_active", _prov()),
        ("location_keys", _prov()),
        ("name", _prov()),
        ("price", _prov()),
        ("requires_resource", _prov()),
    )


def _res_prov() -> FieldProvenance:
    return _fp(
        ("display_name", _prov()),
        ("is_active", _prov()),
        ("location_keys", _prov()),
        ("resource_type", _prov()),
        ("schedule", _prov()),
        ("service_keys", _prov()),
    )


def _prod_prov() -> FieldProvenance:
    return _fp(("is_active", _prov()), ("name", _prov()))


def _price(
    kind: PriceKind = PriceKind.FIXED,
    amount: Decimal | None = Decimal("500.00"),
    currency: str = "INR",
    **kw: object,
) -> PricePolicy:
    return PricePolicy(kind=kind, currency=currency, amount=amount, **kw)


def _schedule() -> WeeklySchedule:
    return WeeklySchedule(
        periods=(
            SchedulePeriod(day=Weekday.MONDAY, start=time(9), end=time(18)),
            SchedulePeriod(day=Weekday.TUESDAY, start=time(9), end=time(18)),
            SchedulePeriod(day=Weekday.SUNDAY, is_closed=True, start=time(0), end=time(0)),
        )
    )


def _loc(key: str = "loc-1") -> LocationDraft:
    return LocationDraft(
        key=key,
        display_name="Lotus Anna Nagar",
        address=AddressComponents(line1="123 Main St", city="Chennai", state="TN"),
        contact=ContactInfo(phone="+919123456789"),
        schedule=_schedule(),
        provenance=_fp(
            ("address.city", _prov()),
            ("address.line1", _prov()),
            ("address.state", _prov()),
            ("contact.phone", _prov()),
            ("display_name", _prov()),
            ("is_active", _prov()),
            ("schedule", _prov()),
        ),
    )


def _svc(
    key: str = "svc-haircut",
    rk: tuple[str, ...] = ("res-anitha",),
) -> ServiceDraft:
    return ServiceDraft(
        key=key,
        name="Haircut",
        location_keys=("loc-1",),
        duration_minutes=30,
        category="hair",
        price=_price(),
        eligible_resource_keys=rk,
        provenance=_svc_prov(),
    )


def _res(key: str = "res-anitha") -> ResourceDraft:
    return ResourceDraft(
        key=key,
        display_name="Anitha",
        resource_type=ResourceType.STAFF,
        location_keys=("loc-1",),
        service_keys=("svc-haircut",),
        schedule=_schedule(),
        provenance=_res_prov(),
    )


def _prod(key: str = "prod-shampoo") -> ProductDraft:
    return ProductDraft(
        key=key,
        name="Shampoo",
        unit="bottle",
        price=_price(),
        location_keys=("loc-1",),
        provenance=_fp(
            ("is_active", _prov()),
            ("location_keys", _prov()),
            ("name", _prov()),
            ("price", _prov()),
            ("unit", _prov()),
        ),
    )


def _salon() -> BusinessOnboardingDraft:
    return BusinessOnboardingDraft(
        draft_id="draft-1",
        business_name="Lotus Salon",
        business_category=BusinessCategory.SALON,
        default_timezone="Asia/Kolkata",
        default_currency="INR",
        preferred_languages=("ta-IN", "en-IN"),
        locations=(_loc(),),
        services=(_svc(),),
        resources=(_res(),),
        provenance=_biz_prov(),
    )


class TestConstruction:
    def test_valid_salon(self) -> None:
        r = validate_draft(_salon())
        assert r.blocker_count == 0

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BusinessOnboardingDraft(draft_id="d1", bad="x")  # type: ignore[call-arg]

    def test_collection_limits(self) -> None:
        locs = tuple(_loc(key=f"loc-{i}") for i in range(MAX_LOCATIONS + 1))
        with pytest.raises(DraftLimitExceededError, match="locations"):
            BusinessOnboardingDraft(draft_id="d1", default_timezone="Asia/Kolkata", locations=locs)

    def test_source_batches_bounded(self) -> None:
        with pytest.raises(DraftLimitExceededError, match="source_batches"):
            BusinessOnboardingDraft(
                draft_id="d1",
                source_batches=tuple(f"b-{i}" for i in range(MAX_SOURCE_BATCHES + 1)),
            )

    def test_with_updates_validates(self) -> None:
        d = _salon()
        u = d.with_updates(business_name="New")
        assert u.business_name == "New"
        with pytest.raises(ValidationError):
            d.with_updates(locations=[{"key": "", "display_name": ""}])

    def test_key_element_bounded(self) -> None:
        with pytest.raises(ValidationError):
            LocationDraft(key="x" * (MAX_KEY_LENGTH + 1), display_name="T")

    def test_key_rejects_whitespace(self) -> None:
        with pytest.raises(ValidationError):
            ServiceDraft(key="svc 1", name="T", provenance=_svc_prov())

    def test_language_element_bounded(self) -> None:
        with pytest.raises(ValueError, match="Invalid lang"):
            BusinessOnboardingDraft(draft_id="d1", preferred_languages=("x" * 300,))

    def test_whitespace_source_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="whitespace"):
            SourceReference(source_type=SourceType.OPERATOR_ENTRY, source_id="   ")


class TestPricing:
    def test_fixed(self) -> None:
        assert _price().amount == Decimal("500.00")

    def test_fixed_with_min_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not specify min"):
            PricePolicy(
                kind=PriceKind.FIXED,
                currency="INR",
                amount=Decimal("1"),
                minimum=Decimal("1"),
            )

    def test_starting_from_with_amount(self) -> None:
        with pytest.raises(ValidationError, match="must not specify amount"):
            PricePolicy(
                kind=PriceKind.STARTING_FROM,
                currency="INR",
                minimum=Decimal("1"),
                amount=Decimal("2"),
            )

    def test_range_with_amount(self) -> None:
        with pytest.raises(ValidationError, match="must not specify amount"):
            PricePolicy(
                kind=PriceKind.RANGE,
                currency="INR",
                minimum=Decimal("1"),
                maximum=Decimal("2"),
                amount=Decimal("1"),
            )

    def test_unsupported_currency(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported"):
            _price(currency="USD")

    def test_numeric_currency(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported"):
            _price(currency="123")


class TestProvenance:
    def test_default_is_missing(self) -> None:
        assert ProvenanceField().review_status is ReviewStatus.MISSING

    def test_clear_without_evidence_fails(self) -> None:
        d = _salon().with_updates(
            provenance=_fp(
                ("business_name", ProvenanceField(review_status=ReviewStatus.CLEAR)),
                ("business_category", _prov()),
                ("default_currency", _prov()),
                ("default_timezone", _prov()),
            ).model_dump(mode="json"),
        )
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "no_evidence" in codes

    def test_correction_preserves_original(self) -> None:
        o = _prov(ReviewStatus.AMBIGUOUS)
        c = o.with_correction("Lotus", _src(SourceType.OWNER_FORM))
        assert c.review_status is ReviewStatus.OWNER_CORRECTED
        assert len(c.sources) == 2

    def test_source_limit_exceeded(self) -> None:
        srcs = tuple(
            SourceReference(source_type=SourceType.OPERATOR_ENTRY, source_id=f"s{i}")
            for i in range(MAX_PROVENANCE_ENTRIES + 1)
        )
        with pytest.raises(DraftLimitExceededError, match="provenance"):
            ProvenanceField(review_status=ReviewStatus.CLEAR, sources=srcs)

    def test_no_provenance_blocks_approval(self) -> None:
        d = BusinessOnboardingDraft(
            draft_id="d1",
            business_name="T",
            default_timezone="Asia/Kolkata",
            locations=(_loc(),),
            services=(
                ServiceDraft(
                    key="s1",
                    name="T",
                    duration_minutes=30,
                    price=_price(),
                    requires_resource=False,
                    provenance=_svc_prov(),
                ),
            ),
        )
        r = validate_draft(d)
        assert r.blocker_count > 0
        codes = {i.code for i in r.blockers}
        assert "missing_field_provenance" in codes

    def test_every_biz_field_requires_evidence(self) -> None:
        d = _salon().with_updates(
            provenance=_fp(
                ("business_name", _prov()),
                ("business_category", _prov()),
                ("default_currency", _prov()),
            ).model_dump(mode="json"),
        )
        r = validate_draft(d)
        assert r.blocker_count > 0

    def test_service_price_provenance(self) -> None:
        svc_p = _fp(
            ("duration_minutes", _prov()),
            ("eligible_resource_keys", _prov()),
            ("is_active", _prov()),
            ("location_keys", _prov()),
            ("name", _prov()),
            ("requires_resource", _prov()),
        )
        d = _salon().with_updates(
            services=[_svc().model_copy(update={"provenance": svc_p}).model_dump(mode="json")]
        )
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "missing_field_provenance" in codes

    def test_address_provenance(self) -> None:
        loc_p = _fp(
            ("contact.phone", _prov()),
            ("display_name", _prov()),
            ("is_active", _prov()),
            ("schedule", _prov()),
        )
        d = _salon().with_updates(
            locations=[_loc().model_copy(update={"provenance": loc_p}).model_dump(mode="json")]
        )
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "missing_field_provenance" in codes

    def test_duplicate_prov_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate"):
            FieldProvenance(fields=(("a", _prov()), ("a", _prov())))


class TestSchedule:
    def test_closed_with_times_rejected(self) -> None:
        with pytest.raises(ValidationError, match="closed period must use"):
            SchedulePeriod(day=Weekday.MONDAY, is_closed=True, start=time(9), end=time(18))

    def test_closed_exception_with_times(self) -> None:
        with pytest.raises(ValidationError, match="must not specify"):
            ScheduleException(date=date(2026, 1, 26), is_closed=True, start=time(9), end=time(14))

    def test_empty_tz_override_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            LocationDraft(key="l1", display_name="T", timezone_override="")

    def test_duplicate_exception_date(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate exception"):
            WeeklySchedule(
                exceptions=(
                    ScheduleException(date=date(2026, 1, 26)),
                    ScheduleException(date=date(2026, 1, 26)),
                )
            )

    def test_overlap(self) -> None:
        d = _salon().with_updates(
            locations=[
                _loc()
                .model_copy(
                    update={
                        "schedule": WeeklySchedule(
                            periods=(
                                SchedulePeriod(day=Weekday.MONDAY, start=time(9), end=time(14)),
                                SchedulePeriod(day=Weekday.MONDAY, start=time(13), end=time(18)),
                            )
                        )
                    }
                )
                .model_dump(mode="json"),
            ]
        )
        r = validate_draft(d)
        codes = {i.code for i in r.issues}
        assert "schedule_overlap" in codes


class TestEligibility:
    def test_empty_resource_service_keys(self) -> None:
        res = _res().model_copy(update={"service_keys": ()})
        d = _salon().with_updates(resources=[res.model_dump(mode="json")])
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "service_no_usable_resource" in codes

    def test_empty_resource_location_keys(self) -> None:
        res = _res().model_copy(update={"location_keys": ()})
        d = _salon().with_updates(resources=[res.model_dump(mode="json")])
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "service_no_usable_resource" in codes

    def test_empty_service_location_keys(self) -> None:
        svc = _svc().model_copy(update={"location_keys": ()})
        d = _salon().with_updates(services=[svc.model_dump(mode="json")])
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "service_no_usable_resource" in codes

    def test_inactive_resource(self) -> None:
        res = _res().model_copy(update={"is_active": False})
        d = _salon().with_updates(resources=[res.model_dump(mode="json")])
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "service_no_usable_resource" in codes

    def test_disjoint_locations(self) -> None:
        loc2 = _loc(key="loc-2")
        res = _res().model_copy(update={"location_keys": ("loc-2",)})
        d = _salon().with_updates(
            locations=[_loc().model_dump(mode="json"), loc2.model_dump(mode="json")],
            resources=[res.model_dump(mode="json")],
        )
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "service_no_usable_resource" in codes

    def test_reciprocal_mismatch(self) -> None:
        res = _res().model_copy(update={"service_keys": ("svc-other",)})
        d = _salon().with_updates(resources=[res.model_dump(mode="json")])
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "service_no_usable_resource" in codes

    def test_resource_free_ok(self) -> None:
        svc = _svc(rk=()).model_copy(update={"requires_resource": False})
        d = _salon().with_updates(services=[svc.model_dump(mode="json")])
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "service_no_usable_resource" not in codes

    def test_valid_explicit_multi_loc(self) -> None:
        loc2 = _loc(key="loc-2")
        svc = _svc().model_copy(update={"location_keys": ("loc-1", "loc-2")})
        res = _res().model_copy(update={"location_keys": ("loc-1", "loc-2")})
        d = _salon().with_updates(
            locations=[_loc().model_dump(mode="json"), loc2.model_dump(mode="json")],
            services=[svc.model_dump(mode="json")],
            resources=[res.model_dump(mode="json")],
        )
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "service_no_usable_resource" not in codes


class TestQuestionPaths:
    def test_multiple_bad_refs_separate_paths(self) -> None:
        svc = ServiceDraft(
            key="svc-1",
            name="S1",
            duration_minutes=30,
            price=_price(),
            location_keys=("loc-1",),
            eligible_resource_keys=("bad1", "bad2"),
            provenance=_svc_prov(),
        )
        d = _salon().with_updates(services=[svc.model_dump(mode="json")])
        r = validate_draft(d)
        ref_issues = [i for i in r.blockers if i.code == "invalid_service_resource_ref"]
        assert len(ref_issues) == 2
        paths = {i.path for i in ref_issues}
        assert len(paths) == 2
        plan = plan_questions(r)
        ref_q = [q for q in plan.questions if q.code == "invalid_service_resource_ref"]
        assert len(ref_q) == 2

    def test_same_code_across_services(self) -> None:
        svc1 = ServiceDraft(
            key="svc-1",
            name="S1",
            duration_minutes=30,
            price=_price(),
            location_keys=("loc-1",),
            eligible_resource_keys=("bad",),
            provenance=_svc_prov(),
        )
        svc2 = ServiceDraft(
            key="svc-2",
            name="S2",
            duration_minutes=30,
            price=_price(),
            location_keys=("loc-1",),
            eligible_resource_keys=("bad2",),
            provenance=_svc_prov(),
        )
        d = _salon().with_updates(
            services=[svc1.model_dump(mode="json"), svc2.model_dump(mode="json")]
        )
        plan = plan_questions(validate_draft(d))
        ref_q = [q for q in plan.questions if q.code == "invalid_service_resource_ref"]
        assert len(ref_q) == 2


class TestDigest:
    def test_reordered_refs_stable(self) -> None:
        s1 = _svc().model_copy(update={"eligible_resource_keys": ("r1", "r2")})
        s2 = _svc().model_copy(update={"eligible_resource_keys": ("r2", "r1")})
        d1 = _salon().with_updates(services=[s1.model_dump(mode="json")])
        d2 = _salon().with_updates(services=[s2.model_dump(mode="json")])
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_reordered_languages_stable(self) -> None:
        d1 = _salon().with_updates(preferred_languages=("ta-IN", "en-IN"))
        d2 = _salon().with_updates(preferred_languages=("en-IN", "ta-IN"))
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_reordered_locations_stable(self) -> None:
        l1 = _loc(key="loc-a")
        l2 = _loc(key="loc-b")
        d1 = _salon().with_updates(
            locations=[l1.model_dump(mode="json"), l2.model_dump(mode="json")]
        )
        d2 = _salon().with_updates(
            locations=[l2.model_dump(mode="json"), l1.model_dump(mode="json")]
        )
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_reordered_services_stable(self) -> None:
        s1 = _svc(key="svc-a")
        s2 = _svc(key="svc-b")
        d1 = _salon().with_updates(
            services=[s1.model_dump(mode="json"), s2.model_dump(mode="json")]
        )
        d2 = _salon().with_updates(
            services=[s2.model_dump(mode="json"), s1.model_dump(mode="json")]
        )
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_changed_fact_changes_digest(self) -> None:
        d1 = _salon()
        d2 = d1.with_updates(business_name="New Salon")
        assert d1.canonical_digest() != d2.canonical_digest()

    def test_status_excluded(self) -> None:
        d1 = _salon()
        d2 = d1.with_updates(status=DraftStatus.APPROVED.value)
        assert d1.canonical_digest() == d2.canonical_digest()

    def test_reordered_provenance_sources(self) -> None:
        s1 = _src(SourceType.OPERATOR_ENTRY)
        s2 = _src(SourceType.IMAGE)
        p1 = _prov(sources=(s1, s2))
        p2 = _prov(sources=(s2, s1))
        d1 = _salon().with_updates(
            provenance=_fp(
                ("business_name", p1),
                ("business_category", _prov()),
                ("default_currency", _prov()),
                ("default_timezone", _prov()),
            ).model_dump(mode="json"),
        )
        d2 = _salon().with_updates(
            provenance=_fp(
                ("business_name", p2),
                ("business_category", _prov()),
                ("default_currency", _prov()),
                ("default_timezone", _prov()),
            ).model_dump(mode="json"),
        )
        assert d1.canonical_digest() == d2.canonical_digest()


class TestIssueAccounting:
    def test_blocker_count_accurate(self) -> None:
        d = BusinessOnboardingDraft(draft_id="d1")
        r = validate_draft(d)
        assert r.blocker_count > 0
        assert r.blocker_count >= len(r.blockers)

    def test_unsupported_schema_counts_all(self) -> None:
        d = BusinessOnboardingDraft(draft_id="d1", schema_version=999)
        r = check_activation_readiness(
            d, approved_digest=d.canonical_digest(), reviewer_ref="owner-1"
        )
        assert r.blocker_count > 1
        assert r.decision is ActivationDecision.BLOCKED_UNSUPPORTED


class TestReview:
    def test_exact_approval(self) -> None:
        d = _salon()
        r = approve_draft(d, reviewer_ref="owner-1", expected_digest=d.canonical_digest())
        assert r.approved

    def test_stale_rejected(self) -> None:
        with pytest.raises(StaleApprovalError):
            approve_draft(_salon(), reviewer_ref="o", expected_digest="stale")

    def test_blockers_reject(self) -> None:
        d = BusinessOnboardingDraft(draft_id="d1")
        with pytest.raises(UnresolvedBlockersError):
            approve_draft(d, reviewer_ref="o", expected_digest=d.canonical_digest())

    def test_empty_reviewer(self) -> None:
        with pytest.raises(InvalidReviewerError):
            approve_draft(_salon(), reviewer_ref="", expected_digest=_salon().canonical_digest())

    def test_whitespace_reviewer(self) -> None:
        with pytest.raises(InvalidReviewerError):
            approve_draft(_salon(), reviewer_ref="   ", expected_digest=_salon().canonical_digest())

    def test_idempotent(self) -> None:
        d = _salon()
        dig = d.canonical_digest()
        r1 = approve_draft(d, reviewer_ref="o", expected_digest=dig)
        r2 = approve_draft(d, reviewer_ref="o", expected_digest=dig)
        assert r1.draft_digest == r2.draft_digest


class TestReadiness:
    def test_ready_salon(self) -> None:
        d = _salon()
        r = check_activation_readiness(d, approved_digest=d.canonical_digest(), reviewer_ref="o")
        assert r.decision is ActivationDecision.REQUIRES_TEST_MODE
        assert r.blocker_count == 0

    def test_no_approval(self) -> None:
        r = check_activation_readiness(_salon(), approved_digest=None, reviewer_ref=None)
        assert r.decision is ActivationDecision.NOT_READY

    def test_stale_approval(self) -> None:
        r = check_activation_readiness(_salon(), approved_digest="stale", reviewer_ref="o")
        assert r.decision is ActivationDecision.NOT_READY

    def test_no_evidence_blocks(self) -> None:
        d = BusinessOnboardingDraft(
            draft_id="d1",
            business_name="T",
            default_timezone="Asia/Kolkata",
            locations=(_loc(),),
        )
        r = check_activation_readiness(d, approved_digest=d.canonical_digest(), reviewer_ref="o")
        assert r.decision is not ActivationDecision.REQUIRES_TEST_MODE

    def test_approved_salon_test_mode(self) -> None:
        d = _salon()
        dig = d.canonical_digest()
        approve_draft(d, reviewer_ref="owner-ref", expected_digest=dig)
        r = check_activation_readiness(d, approved_digest=dig, reviewer_ref="owner-ref")
        assert r.decision is ActivationDecision.REQUIRES_TEST_MODE
        assert r.blocker_count == 0


class TestCurrency:
    def test_unsupported_validation(self) -> None:
        d = _salon().with_updates(default_currency="USD")
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "unsupported_currency" in codes

    def test_numeric_currency(self) -> None:
        d = _salon().with_updates(default_currency="123")
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "unsupported_currency" in codes


class TestTimezone:
    def test_traversal_key(self) -> None:
        d = _salon().with_updates(default_timezone="../../etc/passwd")
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "invalid_timezone" in codes

    def test_empty_missing(self) -> None:
        d = _salon().with_updates(default_timezone="")
        r = validate_draft(d)
        codes = {i.code for i in r.blockers}
        assert "missing_timezone" in codes


class TestPerformance:
    def test_max_sized_draft_bounded(self) -> None:
        locs = tuple(_loc(key=f"loc-{i}") for i in range(MAX_LOCATIONS))
        svcs = tuple(
            ServiceDraft(
                key=f"svc-{i}",
                name=f"S{i}",
                location_keys=(f"loc-{i % MAX_LOCATIONS}",),
                duration_minutes=30,
                price=_price(),
                eligible_resource_keys=(f"res-{i % 10}",),
                provenance=_svc_prov(),
            )
            for i in range(MAX_SERVICES)
        )
        ress = tuple(
            ResourceDraft(
                key=f"res-{i}",
                display_name=f"R{i}",
                location_keys=(f"loc-{i % MAX_LOCATIONS}",),
                service_keys=tuple(f"svc-{j}" for j in range(i, min(i + 5, MAX_SERVICES))),
                provenance=_res_prov(),
            )
            for i in range(50)
        )
        d = BusinessOnboardingDraft(
            draft_id="big",
            business_name="Big",
            default_timezone="Asia/Kolkata",
            locations=locs,
            services=svcs,
            resources=ress,
            provenance=_biz_prov(),
        )
        r = validate_draft(d)
        assert len(r.issues) <= 500
        plan = plan_questions(r)
        assert len(plan.questions) <= 200
