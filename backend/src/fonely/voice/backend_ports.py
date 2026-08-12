"""Adapters from canonical backend domain types to voice runtime ports.

AppointmentServiceCommandPort is the production CommandPort that calls
the real AppointmentService with real PostgreSQL transactions. The test
engine is structurally excluded from this module — it is never imported
here, and receipts with source="test_engine" fail validator check #22
in production (allowed_sources = {"appointment_service"}).

TrustedCommandContext fields come exclusively from the authenticated
session (ActorContext + session config). Model output supplies only
intent and collected facts — never business_id, actor identity, role,
or membership provenance.

Confirmation speech must be derived from the committed receipt's facts
(what the database recorded), not from what the model intended to book.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from fonely.domain.appointments.validation import AppointmentValidationPort

from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCommand,
    CreatePendingAppointmentCommand,
)
from fonely.domain.appointments.results import (
    PreCommitAppointmentFailure,
    PreCommitAppointmentSuccess,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole, Channel

from .context import AvailabilityQuery, AvailableSlot, DayAvailability
from .runtime import CommandResult, CommitReceipt, ConfirmCommand, ProposeCommand

logger = logging.getLogger("fonely.voice.backend_ports")

# Factory aliases: the application injects a session factory (opens an async
# session context manager) and a validation factory (builds the validation port
# for a given session). Typed here so the port's constructor is not a bare
# Callable and mypy can check call sites.
SessionFactory = Callable[[], "AbstractAsyncContextManager[AsyncSession]"]
ValidationFactory = Callable[["AsyncSession"], "AppointmentValidationPort"]


class AppointmentServiceCommandPort:
    """Production CommandPort backed by real AppointmentService + PostgreSQL.

    Every field in TrustedCommandContext originates from the authenticated
    call session. The adapter is physically unable to accept business_id,
    actor role, or membership from model output — those are frozen at
    construction from the ActorContext the application built.
    """

    def __init__(
        self,
        *,
        actor: ActorContext,
        session_factory: SessionFactory,
        validation_factory: ValidationFactory,
        business_timezone: str,
        conversation_id: str,
    ) -> None:
        self._actor = actor
        self._session_factory = session_factory
        self._validation_factory = validation_factory
        self._business_timezone = business_timezone
        self._conversation_id = conversation_id
        self._booking_attempt = 0

    async def propose(self, cmd: ProposeCommand) -> CommandResult:
        self._booking_attempt += 1

        if cmd.target_date is None or not cmd.target_time:
            return CommandResult(success=False, error="incomplete_facts")

        start_at = self._build_start_at(cmd.target_date, cmd.target_time)
        expires_at = datetime.now(UTC) + timedelta(minutes=15)

        idempotency_key = cmd.idempotency_key or (
            f"voice-{self._actor.session_id}-a{self._booking_attempt}"
        )

        try:
            async with self._session_factory() as session:
                from fonely.services.appointments import AppointmentService

                service = AppointmentService(
                    session,
                    validation=self._validation_factory(session),
                )
                result = await service.create_proposal(
                    CreatePendingAppointmentCommand(
                        actor=self._actor,
                        service_id=cmd.service_id or 1,
                        resource_id=cmd.resource_id,
                        start_at=start_at,
                        customer_name=cmd.customer_name or None,
                        customer_phone=self._actor.normalized_phone,
                        reason=None,
                        call_id=None,
                        expires_at=expires_at,
                        idempotency_key=idempotency_key,
                    )
                )
                await session.commit()

                return CommandResult(
                    success=True,
                    operation="create",
                    proposal_id=result.pending_action_id,
                    evidence={
                        "version": result.version,
                        "expires_at": str(result.expires_at),
                    },
                )
        except Exception as exc:
            logger.error(
                "appointment_propose_failed",
                extra={"error": type(exc).__name__},
                exc_info=True,
            )
            return CommandResult(success=False, error=type(exc).__name__)

    async def confirm(self, cmd: ConfirmCommand) -> CommandResult:
        try:
            async with self._session_factory() as session:
                from fonely.services.appointments import AppointmentService

                service = AppointmentService(
                    session,
                    validation=self._validation_factory(session),
                )
                outcome = await service.confirm_and_commit(
                    ConfirmPendingAppointmentCommand(
                        actor=self._actor,
                        pending_action_id=cmd.proposal_id,
                        expected_version=cmd.expected_version or 2,
                    )
                )
                await session.commit()

                if isinstance(outcome, PreCommitAppointmentFailure):
                    return CommandResult(
                        success=False,
                        error=outcome.error_code.value,
                        operation="create",
                        proposal_id=cmd.proposal_id,
                    )

                assert isinstance(outcome, PreCommitAppointmentSuccess)
                appt = outcome.appointment

                receipt = CommitReceipt(
                    commitment_id=appt.appointment_id,
                    proposal_id=appt.pending_action_id,
                    business_id=self._actor.business_id,
                    operation="create",
                    idempotency_key=cmd.idempotency_key or "",
                    confirm_idempotency_key=cmd.idempotency_key or "",
                    payload_digest="",
                    committed_at_ns=time.time_ns(),
                    facts={
                        "service_id": appt.service_id,
                        "service_name": appt.service_name,
                        "resource_id": appt.resource_id,
                        "resource_name": appt.resource_name,
                        "start_at": str(appt.start_at),
                        "end_at": str(appt.end_at),
                        "business_timezone": appt.business_timezone,
                    },
                    source="appointment_service",
                )

                return CommandResult(
                    success=True,
                    operation="create",
                    proposal_id=cmd.proposal_id,
                    committed=True,
                    receipt=receipt,
                )
        except Exception as exc:
            logger.error(
                "appointment_confirm_failed",
                extra={"error": type(exc).__name__},
                exc_info=True,
            )
            return CommandResult(success=False, error=type(exc).__name__)

    def _build_start_at(self, target_date: date, target_time: str) -> datetime:
        parts = target_time.split(":")
        hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        tz = ZoneInfo(self._business_timezone)
        local_dt = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            hour,
            minute,
            tzinfo=tz,
        )
        return local_dt


class AvailabilityServiceAdapter:
    """Adapts backend AvailabilityService to voice AvailabilityPort.

    Queries the real AvailabilityService with trusted business_id,
    service_id, resource_id, and date.
    """

    def __init__(
        self,
        *,
        actor: ActorContext,
        session_factory: SessionFactory,
        default_service_id: int = 1,
        default_resource_id: int | None = None,
    ) -> None:
        self._actor = actor
        self._session_factory = session_factory
        self._default_service_id = default_service_id
        self._default_resource_id = default_resource_id

    async def query_day_availability(self, query: AvailabilityQuery) -> DayAvailability:
        try:
            async with self._session_factory() as session:
                from fonely.services.availability import AvailabilityService

                avail_svc = AvailabilityService(session)

                resource_id = query.resource_id or self._default_resource_id
                if resource_id is None:
                    return DayAvailability(
                        business_date=query.target_date,
                        day_of_week=query.target_date.strftime("%A").lower(),
                        is_operating_day=False,
                        is_exception_day=False,
                        reason="no_resource_id",
                    )

                slots = await avail_svc.get_available_slots(
                    business_id=self._actor.business_id,
                    service_id=query.service_id or self._default_service_id,
                    resource_id=resource_id,
                    target_date=query.target_date,
                )

                tz = ZoneInfo(query.business_timezone) if query.business_timezone else None
                available_slots = tuple(
                    AvailableSlot(
                        resource_id=s.resource_id,
                        resource_name=s.resource_name,
                        start_time=s.start_at.astimezone(tz).time() if tz else s.start_at.time(),
                        end_time=s.end_at.astimezone(tz).time() if tz else s.end_at.time(),
                        service_name="",
                    )
                    for s in slots
                )

                return DayAvailability(
                    business_date=query.target_date,
                    day_of_week=query.target_date.strftime("%A").lower(),
                    is_operating_day=len(available_slots) > 0,
                    is_exception_day=False,
                    available_slots=available_slots,
                )
        except Exception as exc:
            logger.error(
                "availability_query_failed",
                extra={"error": type(exc).__name__},
                exc_info=True,
            )
            return DayAvailability(
                business_date=query.target_date,
                day_of_week=query.target_date.strftime("%A").lower(),
                is_operating_day=False,
                is_exception_day=False,
                reason=f"query_error:{type(exc).__name__}",
            )


def build_actor_context(
    business_id: int,
    phone: str,
    session_id: str,
) -> ActorContext:
    """Build a trusted ActorContext for voice session."""
    return ActorContext(
        business_id=business_id,
        normalized_phone=phone,
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.VOICE,  # voice transport: give-up wording must not say "call the clinic"
        session_id=session_id,
    )
