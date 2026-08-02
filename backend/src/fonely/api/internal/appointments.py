"""Internal text appointment slice routes."""

import logging
import time

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.api.internal.models import (
    AppointmentConfirmRequest,
    AppointmentProposalRequest,
    CommittedAppointmentResponse,
    ErrorResponse,
    ProposalResponse,
    RetryableFailureResponse,
)
from fonely.api.internal.validation import InternalValidationPort
from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCommand,
    CreatePendingAppointmentCommand,
)
from fonely.domain.appointments.results import (
    PreCommitAppointmentFailure,
    PreCommitAppointmentSuccess,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.errors import (
    PendingActionConcurrencyError,
    PendingActionExpiredError,
    PendingActionNotFoundError,
    PendingActionUnauthorizedError,
)
from fonely.models.enums import CallerRole
from fonely.services.appointments import AppointmentService

logger = logging.getLogger("fonely.api.internal.appointments")

router = APIRouter(prefix="/internal/v1", tags=["internal-appointments"])

_MAX_REQUEST_BODY = 4096


def _get_correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", "unknown")


async def _get_session(request: Request) -> AsyncSession:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory()


def _trusted_actor(request: Request) -> ActorContext:
    return ActorContext(
        business_id=int(request.headers.get("X-Business-ID", "0")),
        normalized_phone=request.headers.get("X-Actor-Phone", ""),
        verified_role=CallerRole(request.headers.get("X-Actor-Role", "customer")),
        session_id=request.headers.get("X-Session-ID"),
    )


@router.post(
    "/appointment-proposals",
    response_model=ProposalResponse,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def create_proposal(
    body: AppointmentProposalRequest,
    request: Request,
) -> ProposalResponse | ErrorResponse:
    correlation_id = _get_correlation_id(request)
    actor = _trusted_actor(request)
    start = time.monotonic()

    if actor.business_id <= 0:
        raise HTTPException(status_code=400, detail="Missing trusted business context")

    session = await _get_session(request)
    try:
        validation = InternalValidationPort(session)
        service = AppointmentService(session, validation=validation)

        proposal = await service.create_proposal(
            CreatePendingAppointmentCommand(
                actor=actor,
                service_id=body.service_id,
                resource_id=body.resource_id,
                start_at=body.start_at,
                customer_name=body.customer_name,
                customer_phone=body.customer_phone,
                reason=body.reason,
                call_id=body.call_id,
                expires_at=body.expires_at,
                idempotency_key=body.idempotency_key,
            )
        )
        await session.commit()

        duration = time.monotonic() - start
        logger.info(
            "proposal_created",
            extra={
                "correlation_id": correlation_id,
                "operation": "create_proposal",
                "business_id": actor.business_id,
                "pending_action_id": proposal.pending_action_id,
                "duration_ms": round(duration * 1000),
            },
        )

        return ProposalResponse(
            correlation_id=correlation_id,
            status=proposal.status,
            pending_action_id=proposal.pending_action_id,
            version=proposal.version,
            expires_at=proposal.expires_at,
            slot_is_held=proposal.slot_is_held,
            confirmation_facts=proposal.confirmation_facts.model_dump(),
        )
    except (PendingActionNotFoundError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PendingActionUnauthorizedError as exc:
        await session.rollback()
        raise HTTPException(status_code=403, detail="Unauthorized") from exc
    except Exception:
        await session.rollback()
        logger.exception(
            "proposal_failed",
            extra={"correlation_id": correlation_id, "operation": "create_proposal"},
        )
        raise HTTPException(status_code=500, detail="Internal error") from None
    finally:
        await session.close()


@router.post(
    "/appointment-proposals/{pending_action_id}/confirm",
    response_model=CommittedAppointmentResponse | RetryableFailureResponse,
    status_code=200,
    responses={
        400: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def confirm_proposal(
    pending_action_id: int,
    body: AppointmentConfirmRequest,
    request: Request,
) -> CommittedAppointmentResponse | RetryableFailureResponse:
    correlation_id = _get_correlation_id(request)
    actor = _trusted_actor(request)
    start = time.monotonic()

    if actor.business_id <= 0:
        raise HTTPException(status_code=400, detail="Missing trusted business context")

    session = await _get_session(request)
    try:
        validation = InternalValidationPort(session)
        service = AppointmentService(session, validation=validation)

        result = await service.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=actor,
                pending_action_id=pending_action_id,
                expected_version=body.expected_version,
            )
        )

        if isinstance(result, PreCommitAppointmentFailure):
            await session.commit()
            duration = time.monotonic() - start
            logger.info(
                "confirmation_retryable",
                extra={
                    "correlation_id": correlation_id,
                    "operation": "confirm_proposal",
                    "business_id": actor.business_id,
                    "error_code": result.error_code,
                    "retryable": True,
                    "duration_ms": round(duration * 1000),
                },
            )
            return RetryableFailureResponse(
                correlation_id=correlation_id,
                error_code=str(result.error_code),
                pending_action_id=result.pending_action_id,
                pending_action_version=result.pending_action_version,
            )

        assert isinstance(result, PreCommitAppointmentSuccess)
        await session.commit()

        duration = time.monotonic() - start
        logger.info(
            "appointment_committed",
            extra={
                "correlation_id": correlation_id,
                "operation": "confirm_proposal",
                "business_id": actor.business_id,
                "appointment_id": result.appointment.appointment_id,
                "duration_ms": round(duration * 1000),
            },
        )

        return CommittedAppointmentResponse(
            correlation_id=correlation_id,
            appointment_id=result.appointment.appointment_id,
            pending_action_id=result.appointment.pending_action_id,
            service_name=result.appointment.service_name,
            resource_name=result.appointment.resource_name,
            start_at=result.appointment.start_at,
            end_at=result.appointment.end_at,
            business_timezone=result.appointment.business_timezone,
        )
    except PendingActionNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Action not found") from exc
    except PendingActionUnauthorizedError as exc:
        await session.rollback()
        raise HTTPException(status_code=403, detail="Unauthorized") from exc
    except PendingActionExpiredError as exc:
        await session.rollback()
        raise HTTPException(status_code=410, detail="Proposal expired") from exc
    except PendingActionConcurrencyError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Version conflict") from exc
    except Exception:
        await session.rollback()
        logger.exception(
            "confirmation_failed",
            extra={
                "correlation_id": correlation_id,
                "operation": "confirm_proposal",
            },
        )
        raise HTTPException(status_code=500, detail="Internal error") from None
    finally:
        await session.close()
