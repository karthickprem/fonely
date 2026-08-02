"""PostgreSQL integration tests for onboarding persistence lifecycle."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.onboarding.commands import (
    DraftTransitionCommand,
    GetDraftQuery,
    SubmitDraftCommand,
)
from fonely.services.onboarding import (
    OnboardingInvalidTransitionError,
    OnboardingNotFoundError,
    OnboardingService,
    OnboardingStaleVersionError,
    OnboardingUnauthorizedError,
)
from tests.fixtures.dental_clinic import DENTAL_CLINIC_DRAFT

pytestmark = pytest.mark.postgres


async def _seed_business(session: AsyncSession, business_id: int = 1) -> int:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:id, 'Smile Dental', 'clinic', '+914428350001', "
            "'Asia/Kolkata', 'trial')"
        ),
        {"id": business_id},
    )
    result = await session.scalar(
        text(
            "INSERT INTO business_users "
            "(business_id, phone, role, is_active) VALUES "
            "(:bid, '+914428350001', 'owner', true) RETURNING id"
        ),
        {"bid": business_id},
    )
    assert result is not None
    return int(result)


async def test_full_onboarding_lifecycle(pg_session: AsyncSession) -> None:
    owner_id = await _seed_business(pg_session)
    service = OnboardingService(pg_session)

    draft_result = await service.submit_draft(
        SubmitDraftCommand(business_id=1, draft_data=DENTAL_CLINIC_DRAFT)
    )
    assert draft_result.status == "draft"
    assert draft_result.idempotent_replay is False
    draft_id = draft_result.id

    replay = await service.submit_draft(
        SubmitDraftCommand(business_id=1, draft_data=DENTAL_CLINIC_DRAFT)
    )
    assert replay.idempotent_replay is True
    assert replay.id == draft_id

    review_result = await service.submit_for_review(
        DraftTransitionCommand(
            business_id=1,
            draft_id=draft_id,
            actor_user_id=owner_id,
            expected_version=1,
        )
    )
    assert review_result.status == "pending_review"

    approve_result = await service.approve_draft(
        DraftTransitionCommand(
            business_id=1,
            draft_id=draft_id,
            actor_user_id=owner_id,
            expected_version=2,
        )
    )
    assert approve_result.status == "approved"

    activation = await service.activate_configuration(
        DraftTransitionCommand(
            business_id=1,
            draft_id=draft_id,
            actor_user_id=owner_id,
            expected_version=3,
        )
    )
    assert activation.success is True
    assert activation.evidence is not None
    assert activation.evidence.services_count == 5
    assert activation.evidence.resources_count == 2
    assert activation.evidence.eligibilities_count > 0

    svc_count = await pg_session.scalar(text("SELECT count(*) FROM services WHERE business_id = 1"))
    assert svc_count == 5

    res_count = await pg_session.scalar(
        text("SELECT count(*) FROM resources WHERE business_id = 1")
    )
    assert res_count == 2

    elig_count = await pg_session.scalar(
        text("SELECT count(*) FROM service_resource_eligibility WHERE business_id = 1")
    )
    assert elig_count is not None and elig_count > 0

    commit_count = await pg_session.scalar(
        text("SELECT count(*) FROM business_configuration_commits WHERE business_id = 1")
    )
    assert commit_count == 1


async def test_optimistic_version_conflict(pg_session: AsyncSession) -> None:
    owner_id = await _seed_business(pg_session)
    service = OnboardingService(pg_session)

    draft = await service.submit_draft(
        SubmitDraftCommand(business_id=1, draft_data=DENTAL_CLINIC_DRAFT)
    )

    with pytest.raises(OnboardingStaleVersionError):
        await service.submit_for_review(
            DraftTransitionCommand(
                business_id=1,
                draft_id=draft.id,
                actor_user_id=owner_id,
                expected_version=999,
            )
        )


async def test_reject_from_draft(pg_session: AsyncSession) -> None:
    owner_id = await _seed_business(pg_session)
    service = OnboardingService(pg_session)

    draft = await service.submit_draft(
        SubmitDraftCommand(business_id=1, draft_data=DENTAL_CLINIC_DRAFT)
    )

    rejected = await service.reject_draft(
        DraftTransitionCommand(
            business_id=1,
            draft_id=draft.id,
            actor_user_id=owner_id,
            expected_version=1,
        )
    )
    assert rejected.status == "rejected"


async def test_reject_from_approved_fails(pg_session: AsyncSession) -> None:
    owner_id = await _seed_business(pg_session)
    service = OnboardingService(pg_session)

    draft = await service.submit_draft(
        SubmitDraftCommand(business_id=1, draft_data=DENTAL_CLINIC_DRAFT)
    )
    await service.submit_for_review(
        DraftTransitionCommand(
            business_id=1,
            draft_id=draft.id,
            actor_user_id=owner_id,
            expected_version=1,
        )
    )
    await service.approve_draft(
        DraftTransitionCommand(
            business_id=1,
            draft_id=draft.id,
            actor_user_id=owner_id,
            expected_version=2,
        )
    )

    with pytest.raises(OnboardingInvalidTransitionError):
        await service.reject_draft(
            DraftTransitionCommand(
                business_id=1,
                draft_id=draft.id,
                actor_user_id=owner_id,
                expected_version=3,
            )
        )


async def test_cross_tenant_isolation(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session, business_id=1)
    await _seed_business(pg_session, business_id=2)
    service = OnboardingService(pg_session)

    draft = await service.submit_draft(
        SubmitDraftCommand(business_id=1, draft_data=DENTAL_CLINIC_DRAFT)
    )

    with pytest.raises(OnboardingNotFoundError):
        await service.get_draft(GetDraftQuery(business_id=2, draft_id=draft.id))


async def test_approve_requires_owner(pg_session: AsyncSession) -> None:
    await _seed_business(pg_session)
    manager_id = await pg_session.scalar(
        text(
            "INSERT INTO business_users "
            "(business_id, phone, role, is_active) VALUES "
            "(1, '+919999999999', 'manager', true) RETURNING id"
        )
    )
    assert manager_id is not None
    service = OnboardingService(pg_session)

    draft = await service.submit_draft(
        SubmitDraftCommand(business_id=1, draft_data=DENTAL_CLINIC_DRAFT)
    )
    owner_id = await pg_session.scalar(
        text("SELECT id FROM business_users WHERE business_id=1 AND role='owner'")
    )
    assert owner_id is not None
    await service.submit_for_review(
        DraftTransitionCommand(
            business_id=1,
            draft_id=draft.id,
            actor_user_id=int(owner_id),
            expected_version=1,
        )
    )

    with pytest.raises(OnboardingUnauthorizedError):
        await service.approve_draft(
            DraftTransitionCommand(
                business_id=1,
                draft_id=draft.id,
                actor_user_id=int(manager_id),
                expected_version=2,
            )
        )
