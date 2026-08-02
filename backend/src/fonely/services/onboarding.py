"""Onboarding persistence service within a caller-owned session transaction."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.onboarding.commands import (
    DraftTransitionCommand,
    GetDraftQuery,
    SubmitDraftCommand,
)
from fonely.domain.onboarding.errors import OnboardingError
from fonely.domain.onboarding.models import BusinessOnboardingDraft as DomainDraft
from fonely.domain.onboarding.persistence_results import (
    ActivationEvidence,
    ActivationResult,
    OnboardingDraftResult,
)
from fonely.domain.onboarding.validation import validate_draft
from fonely.models.enums import BusinessUserRole, OnboardingDraftStatus
from fonely.models.schema import BusinessUser
from fonely.repositories.onboarding import OnboardingRepository


class OnboardingStaleVersionError(OnboardingError):
    code = "onboarding_stale_version"


class OnboardingNotFoundError(OnboardingError):
    code = "onboarding_not_found"


class OnboardingInvalidTransitionError(OnboardingError):
    code = "onboarding_invalid_transition"


class OnboardingUnauthorizedError(OnboardingError):
    code = "onboarding_unauthorized"


class OnboardingValidationError(OnboardingError):
    code = "onboarding_validation_failed"


class OnboardingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = OnboardingRepository(session)

    async def submit_draft(self, command: SubmitDraftCommand) -> OnboardingDraftResult:
        draft = DomainDraft(**command.draft_data)
        digest = draft.canonical_digest()

        existing = await self._repo.get_draft_by_digest(command.business_id, digest)
        if existing is not None:
            return self._to_result(existing, idempotent_replay=True)

        stored = await self._repo.insert_draft(
            {
                "business_id": command.business_id,
                "status": OnboardingDraftStatus.DRAFT.value,
                "draft_data": command.draft_data,
                "draft_digest": digest,
                "submitted_by_user_id": command.actor_user_id,
            }
        )
        return self._to_result(stored)

    async def submit_for_review(self, command: DraftTransitionCommand) -> OnboardingDraftResult:
        draft = await self._require_draft(command.business_id, command.draft_id)
        if draft.status != OnboardingDraftStatus.DRAFT.value:
            raise OnboardingInvalidTransitionError("Only drafts can be submitted for review")

        domain_draft = DomainDraft(**draft.draft_data)
        result = validate_draft(domain_draft)
        if result.blocker_count > 0:
            raise OnboardingValidationError(f"Draft has {result.blocker_count} unresolved blockers")

        updated = await self._repo.update_draft_status(
            command.draft_id,
            command.business_id,
            command.expected_version,
            status=OnboardingDraftStatus.PENDING_REVIEW.value,
        )
        if updated is None:
            raise OnboardingStaleVersionError("Draft version conflict")
        return self._to_result(updated)

    async def approve_draft(self, command: DraftTransitionCommand) -> OnboardingDraftResult:
        draft = await self._require_draft(command.business_id, command.draft_id)
        if draft.status != OnboardingDraftStatus.PENDING_REVIEW.value:
            raise OnboardingInvalidTransitionError("Only drafts pending review can be approved")

        await self._require_owner(command.business_id, command.actor_user_id)

        from fonely.domain.onboarding.review import approve_draft as domain_approve

        domain_draft = DomainDraft(**draft.draft_data)
        domain_approve(
            domain_draft,
            reviewer_ref=f"user:{command.actor_user_id}",
            expected_digest=draft.draft_digest,
        )

        from sqlalchemy.sql import func

        updated = await self._repo.update_draft_status(
            command.draft_id,
            command.business_id,
            command.expected_version,
            status=OnboardingDraftStatus.APPROVED.value,
            reviewed_by_user_id=command.actor_user_id,
            approved_at=func.now(),
        )
        if updated is None:
            raise OnboardingStaleVersionError("Draft version conflict")
        return self._to_result(updated)

    async def activate_configuration(self, command: DraftTransitionCommand) -> ActivationResult:
        draft = await self._require_draft(command.business_id, command.draft_id)
        if draft.status != OnboardingDraftStatus.APPROVED.value:
            raise OnboardingInvalidTransitionError("Only approved drafts can be activated")

        domain_draft = DomainDraft(**draft.draft_data)
        current_digest = domain_draft.canonical_digest()
        if current_digest != draft.draft_digest:
            raise OnboardingStaleVersionError("Draft digest mismatch")

        try:
            async with self._session.begin_nested():
                evidence = await self._write_configuration(command.business_id, domain_draft)

                commit = await self._repo.insert_commit(
                    {
                        "business_id": command.business_id,
                        "onboarding_draft_id": command.draft_id,
                        "draft_digest": draft.draft_digest,
                        "committed_by_user_id": command.actor_user_id,
                        "commit_evidence": evidence.model_dump(),
                    }
                )

                from sqlalchemy.sql import func

                updated = await self._repo.update_draft_status(
                    command.draft_id,
                    command.business_id,
                    command.expected_version,
                    status=OnboardingDraftStatus.ACTIVATED.value,
                    activated_at=func.now(),
                )
                if updated is None:
                    raise OnboardingStaleVersionError("Draft version conflict during activation")

        except OnboardingStaleVersionError:
            raise
        except Exception as exc:
            await self._repo.insert_commit(
                {
                    "business_id": command.business_id,
                    "onboarding_draft_id": command.draft_id,
                    "draft_digest": draft.draft_digest,
                    "committed_by_user_id": command.actor_user_id,
                    "commit_evidence": {},
                    "rollback_evidence": {"error": str(exc)[:500]},
                }
            )
            return ActivationResult(
                draft_id=command.draft_id,
                business_id=command.business_id,
                success=False,
                error=str(exc)[:500],
            )

        return ActivationResult(
            draft_id=command.draft_id,
            business_id=command.business_id,
            success=True,
            commit_id=commit.id,
            evidence=evidence,
        )

    async def reject_draft(self, command: DraftTransitionCommand) -> OnboardingDraftResult:
        draft = await self._require_draft(command.business_id, command.draft_id)
        if draft.status not in (
            OnboardingDraftStatus.DRAFT.value,
            OnboardingDraftStatus.PENDING_REVIEW.value,
        ):
            raise OnboardingInvalidTransitionError(
                "Only draft or pending_review drafts can be rejected"
            )

        updated = await self._repo.update_draft_status(
            command.draft_id,
            command.business_id,
            command.expected_version,
            status=OnboardingDraftStatus.REJECTED.value,
        )
        if updated is None:
            raise OnboardingStaleVersionError("Draft version conflict")
        return self._to_result(updated)

    async def get_draft(self, query: GetDraftQuery) -> OnboardingDraftResult:
        draft = await self._require_draft(query.business_id, query.draft_id)
        return self._to_result(draft)

    async def _write_configuration(
        self, business_id: int, draft: DomainDraft
    ) -> ActivationEvidence:
        services_count = 0
        resources_count = 0
        eligibilities_count = 0
        schedules_count = 0
        exceptions_count = 0

        service_id_map: dict[str, int] = {}
        resource_id_map: dict[str, int] = {}

        for svc in draft.services:
            price = None
            if svc.price is not None and svc.price.amount is not None:
                price = svc.price.amount
            elif svc.price is not None and svc.price.minimum is not None:
                price = svc.price.minimum

            service = await self._repo.upsert_service(
                business_id,
                svc.name,
                duration_minutes=svc.duration_minutes or 30,
                buffer_before_minutes=svc.buffer_before_minutes,
                buffer_after_minutes=svc.buffer_after_minutes,
                price=Decimal(str(price)) if price is not None else None,
                is_active=svc.is_active,
            )
            service_id_map[svc.key] = service.id
            services_count += 1

        for res in draft.resources:
            resource = await self._repo.upsert_resource(
                business_id,
                res.display_name,
                resource_type=(
                    res.resource_type.value
                    if hasattr(res.resource_type, "value")
                    else str(res.resource_type)
                ),
                is_active=res.is_active,
            )
            resource_id_map[res.key] = resource.id
            resources_count += 1

        for svc in draft.services:
            svc_id = service_id_map.get(svc.key)
            if svc_id is None:
                continue
            for res_key in svc.eligible_resource_keys:
                res_id = resource_id_map.get(res_key)
                if res_id is None:
                    continue
                await self._repo.upsert_eligibility(business_id, svc_id, res_id)
                eligibilities_count += 1

        for loc in draft.locations:
            for period in loc.schedule.periods:
                if period.is_closed:
                    continue
                await self._repo.upsert_schedule(
                    business_id,
                    None,
                    period.day.value,
                    period.start,
                    period.end,
                )
                schedules_count += 1

            for exc in loc.schedule.exceptions:
                await self._repo.upsert_exception(
                    business_id,
                    None,
                    exc.date,
                    is_closed=exc.is_closed,
                    open_time=exc.start if not exc.is_closed else None,
                    close_time=exc.end if not exc.is_closed else None,
                    reason=exc.reason,
                )
                exceptions_count += 1

        for res in draft.resources:
            res_id = resource_id_map.get(res.key)
            if res_id is None:
                continue
            for period in res.schedule.periods:
                if period.is_closed:
                    continue
                await self._repo.upsert_schedule(
                    business_id,
                    res_id,
                    period.day.value,
                    period.start,
                    period.end,
                )
                schedules_count += 1

            for exc in res.schedule.exceptions:
                await self._repo.upsert_exception(
                    business_id,
                    res_id,
                    exc.date,
                    is_closed=exc.is_closed,
                    open_time=exc.start if not exc.is_closed else None,
                    close_time=exc.end if not exc.is_closed else None,
                    reason=exc.reason,
                )
                exceptions_count += 1

        return ActivationEvidence(
            services_count=services_count,
            resources_count=resources_count,
            eligibilities_count=eligibilities_count,
            schedules_count=schedules_count,
            exceptions_count=exceptions_count,
        )

    async def _require_draft(self, business_id: int, draft_id: int) -> Any:
        draft = await self._repo.get_draft(business_id, draft_id)
        if draft is None:
            raise OnboardingNotFoundError("Onboarding draft not found")
        return draft

    async def _require_owner(self, business_id: int, user_id: int) -> None:
        user = await self._session.scalar(
            select(BusinessUser).where(
                BusinessUser.business_id == business_id,
                BusinessUser.id == user_id,
                BusinessUser.is_active.is_(True),
                BusinessUser.role == BusinessUserRole.OWNER.value,
            )
        )
        if user is None:
            raise OnboardingUnauthorizedError("Only business owners can approve drafts")

    @staticmethod
    def _to_result(draft: object, idempotent_replay: bool = False) -> OnboardingDraftResult:
        return OnboardingDraftResult(
            id=getattr(draft, "id", 0),
            business_id=getattr(draft, "business_id", 0),
            status=getattr(draft, "status", ""),
            draft_digest=getattr(draft, "draft_digest", ""),
            version=getattr(draft, "version", 0),
            created_at=getattr(draft, "created_at", datetime.min),
            updated_at=getattr(draft, "updated_at", datetime.min),
            approved_at=getattr(draft, "approved_at", None),
            activated_at=getattr(draft, "activated_at", None),
            idempotent_replay=idempotent_replay,
        )
