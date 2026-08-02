"""Internal onboarding configuration routes."""

import hmac
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.core.config import settings
from fonely.domain.onboarding.commands import (
    DraftTransitionCommand,
    GetDraftQuery,
    SubmitDraftCommand,
)
from fonely.domain.onboarding.errors import OnboardingError
from fonely.services.onboarding import (
    OnboardingInvalidTransitionError,
    OnboardingNotFoundError,
    OnboardingService,
    OnboardingStaleVersionError,
    OnboardingUnauthorizedError,
    OnboardingValidationError,
)

logger = logging.getLogger("fonely.api.internal.onboarding")

router = APIRouter(prefix="/internal/v1", tags=["internal-onboarding"])


class SubmitDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_data: dict[str, Any]


class StatusTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(gt=0)


class OnboardingDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    business_id: int
    status: str
    draft_digest: str
    version: int
    idempotent_replay: bool = False


class ActivationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    commit_id: int | None = None
    services_count: int | None = None
    resources_count: int | None = None
    eligibilities_count: int | None = None
    schedules_count: int | None = None
    exceptions_count: int | None = None
    error: str | None = None


def _verify_internal_auth(request: Request) -> None:
    secret = settings.internal_api_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Internal API not configured")
    provided = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _get_business_id(request: Request) -> int:
    raw = request.headers.get("X-Business-ID", "0")
    try:
        bid = int(raw)
    except ValueError:
        bid = 0
    if bid <= 0:
        raise HTTPException(status_code=400, detail="Missing trusted business context")
    return bid


def _get_actor_user_id(request: Request) -> int:
    raw = request.headers.get("X-Actor-User-ID", "0")
    try:
        uid = int(raw)
    except ValueError:
        uid = 0
    if uid <= 0:
        raise HTTPException(status_code=400, detail="Missing actor user ID")
    return uid


async def _get_session(request: Request) -> AsyncSession:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory()


def _map_error(exc: OnboardingError) -> HTTPException:
    if isinstance(exc, OnboardingNotFoundError):
        return HTTPException(status_code=404, detail="Draft not found")
    if isinstance(exc, OnboardingUnauthorizedError):
        return HTTPException(status_code=403, detail="Not authorized")
    if isinstance(exc, OnboardingStaleVersionError):
        return HTTPException(status_code=409, detail="Version conflict")
    if isinstance(exc, OnboardingInvalidTransitionError):
        return HTTPException(status_code=422, detail="Invalid status transition")
    if isinstance(exc, OnboardingValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="Internal error")


@router.post("/onboarding/drafts", response_model=OnboardingDraftResponse, status_code=201)
async def submit_draft(body: SubmitDraftRequest, request: Request) -> OnboardingDraftResponse:
    _verify_internal_auth(request)
    business_id = _get_business_id(request)
    session = await _get_session(request)
    try:
        service = OnboardingService(session)
        result = await service.submit_draft(
            SubmitDraftCommand(
                business_id=business_id,
                actor_user_id=None,
                draft_data=body.draft_data,
            )
        )
        await session.commit()
        return OnboardingDraftResponse(
            id=result.id,
            business_id=result.business_id,
            status=result.status,
            draft_digest=result.draft_digest,
            version=result.version,
            idempotent_replay=result.idempotent_replay,
        )
    except OnboardingError as exc:
        await session.rollback()
        raise _map_error(exc) from exc
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@router.post(
    "/onboarding/drafts/{draft_id}/submit-review",
    response_model=OnboardingDraftResponse,
)
async def submit_for_review(
    draft_id: int, body: StatusTransitionRequest, request: Request
) -> OnboardingDraftResponse:
    _verify_internal_auth(request)
    business_id = _get_business_id(request)
    actor_user_id = _get_actor_user_id(request)
    session = await _get_session(request)
    try:
        service = OnboardingService(session)
        result = await service.submit_for_review(
            DraftTransitionCommand(
                business_id=business_id,
                draft_id=draft_id,
                actor_user_id=actor_user_id,
                expected_version=body.expected_version,
            )
        )
        await session.commit()
        return OnboardingDraftResponse(
            id=result.id,
            business_id=result.business_id,
            status=result.status,
            draft_digest=result.draft_digest,
            version=result.version,
        )
    except OnboardingError as exc:
        await session.rollback()
        raise _map_error(exc) from exc
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@router.post(
    "/onboarding/drafts/{draft_id}/approve",
    response_model=OnboardingDraftResponse,
)
async def approve_draft(
    draft_id: int, body: StatusTransitionRequest, request: Request
) -> OnboardingDraftResponse:
    _verify_internal_auth(request)
    business_id = _get_business_id(request)
    actor_user_id = _get_actor_user_id(request)
    session = await _get_session(request)
    try:
        service = OnboardingService(session)
        result = await service.approve_draft(
            DraftTransitionCommand(
                business_id=business_id,
                draft_id=draft_id,
                actor_user_id=actor_user_id,
                expected_version=body.expected_version,
            )
        )
        await session.commit()
        return OnboardingDraftResponse(
            id=result.id,
            business_id=result.business_id,
            status=result.status,
            draft_digest=result.draft_digest,
            version=result.version,
        )
    except OnboardingError as exc:
        await session.rollback()
        raise _map_error(exc) from exc
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@router.post(
    "/onboarding/drafts/{draft_id}/activate",
    response_model=ActivationResponse,
)
async def activate_configuration(
    draft_id: int, body: StatusTransitionRequest, request: Request
) -> ActivationResponse:
    _verify_internal_auth(request)
    business_id = _get_business_id(request)
    actor_user_id = _get_actor_user_id(request)
    session = await _get_session(request)
    try:
        service = OnboardingService(session)
        result = await service.activate_configuration(
            DraftTransitionCommand(
                business_id=business_id,
                draft_id=draft_id,
                actor_user_id=actor_user_id,
                expected_version=body.expected_version,
            )
        )
        await session.commit()
        resp = ActivationResponse(success=result.success, commit_id=result.commit_id)
        if result.evidence is not None:
            resp = ActivationResponse(
                success=result.success,
                commit_id=result.commit_id,
                services_count=result.evidence.services_count,
                resources_count=result.evidence.resources_count,
                eligibilities_count=result.evidence.eligibilities_count,
                schedules_count=result.evidence.schedules_count,
                exceptions_count=result.evidence.exceptions_count,
            )
        return resp
    except OnboardingError as exc:
        await session.rollback()
        raise _map_error(exc) from exc
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@router.post(
    "/onboarding/drafts/{draft_id}/reject",
    response_model=OnboardingDraftResponse,
)
async def reject_draft(
    draft_id: int, body: StatusTransitionRequest, request: Request
) -> OnboardingDraftResponse:
    _verify_internal_auth(request)
    business_id = _get_business_id(request)
    actor_user_id = _get_actor_user_id(request)
    session = await _get_session(request)
    try:
        service = OnboardingService(session)
        result = await service.reject_draft(
            DraftTransitionCommand(
                business_id=business_id,
                draft_id=draft_id,
                actor_user_id=actor_user_id,
                expected_version=body.expected_version,
            )
        )
        await session.commit()
        return OnboardingDraftResponse(
            id=result.id,
            business_id=result.business_id,
            status=result.status,
            draft_digest=result.draft_digest,
            version=result.version,
        )
    except OnboardingError as exc:
        await session.rollback()
        raise _map_error(exc) from exc
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@router.get(
    "/onboarding/drafts/{draft_id}",
    response_model=OnboardingDraftResponse,
)
async def get_draft(draft_id: int, request: Request) -> OnboardingDraftResponse:
    _verify_internal_auth(request)
    business_id = _get_business_id(request)
    session = await _get_session(request)
    try:
        service = OnboardingService(session)
        result = await service.get_draft(GetDraftQuery(business_id=business_id, draft_id=draft_id))
        return OnboardingDraftResponse(
            id=result.id,
            business_id=result.business_id,
            status=result.status,
            draft_digest=result.draft_digest,
            version=result.version,
        )
    except OnboardingError as exc:
        raise _map_error(exc) from exc
    finally:
        await session.close()
