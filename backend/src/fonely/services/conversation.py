"""Conversation orchestrator for dental appointment booking."""

import asyncio
import logging
import re
import time
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.config import settings
from fonely.core.validators import utcnow
from fonely.domain.conversation.safety import (
    ESCALATION_MEDICAL,
    ESCALATION_URGENT,
    SafetyClassification,
    classify_intent,
    detect_confirmation,
)
from fonely.domain.conversation.sanitize import sanitize_llm_response
from fonely.domain.conversation.state import (
    ConversationContext,
    ConversationIntent,
    ConversationState,
    ConversationTurn,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.services.model_gateway import ModelGateway, ModelResponse

logger = logging.getLogger("fonely.services.conversation")

_CONVERSATIONS: dict[str, ConversationContext] = {}
_CONVERSATION_LOCKS: dict[str, asyncio.Lock] = {}
_MAX_CONVERSATIONS = 1000
_CONVERSATION_TTL_SECONDS = 3600

_TIMEOUT_RESPONSE = "I'm having trouble right now. Please try again or call the clinic directly."


def _evict_stale() -> None:
    now = utcnow()
    stale = [
        cid
        for cid, ctx in _CONVERSATIONS.items()
        if (now - ctx.created_at).total_seconds() > _CONVERSATION_TTL_SECONDS
    ]
    for cid in stale:
        del _CONVERSATIONS[cid]
        _CONVERSATION_LOCKS.pop(cid, None)
    if stale:
        logger.info("conversations_evicted", extra={"count": len(stale)})

    if len(_CONVERSATIONS) >= _MAX_CONVERSATIONS:
        sorted_convs = sorted(_CONVERSATIONS.items(), key=lambda x: x[1].created_at)
        to_remove = len(_CONVERSATIONS) - _MAX_CONVERSATIONS + 1
        for cid, _ in sorted_convs[:to_remove]:
            del _CONVERSATIONS[cid]
            _CONVERSATION_LOCKS.pop(cid, None)
        logger.info("conversations_evicted_capacity", extra={"count": to_remove})


def _get_lock(conversation_id: str) -> asyncio.Lock:
    lock = _CONVERSATION_LOCKS.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _CONVERSATION_LOCKS[conversation_id] = lock
    return lock


_REQUIRED_FACTS = ("service_id", "resource_id", "start_at", "customer_phone")


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        model: ModelGateway,
        *,
        appointment_service: "object | None" = None,
    ) -> None:
        self._session = session
        self._model = model
        self._appointment_service = appointment_service

    async def process_message(
        self,
        conversation_id: str,
        business_id: int,
        actor: ActorContext,
        user_message: str,
    ) -> ConversationTurn:
        lock = _get_lock(conversation_id)
        async with lock:
            try:
                async with asyncio.timeout(settings.conversation_timeout_seconds):
                    return await self._process_inner(
                        conversation_id, business_id, actor, user_message
                    )
            except TimeoutError:
                logger.warning(
                    "conversation_timeout",
                    extra={"conversation_id": conversation_id},
                )
                ctx = _CONVERSATIONS.get(conversation_id)
                if ctx is None:
                    ctx = ConversationContext(
                        conversation_id=conversation_id,
                        business_id=business_id,
                    )
                return self._make_turn(
                    ctx,
                    user_message,
                    _TIMEOUT_RESPONSE,
                    ConversationIntent.UNKNOWN,
                    "administrative",
                )

    async def _process_inner(
        self,
        conversation_id: str,
        business_id: int,
        actor: ActorContext,
        user_message: str,
    ) -> ConversationTurn:
        start_time = time.monotonic()

        ctx = _CONVERSATIONS.get(conversation_id)
        if ctx is None:
            ctx = ConversationContext(
                conversation_id=conversation_id,
                business_id=business_id,
            )
            _CONVERSATIONS[conversation_id] = ctx

        if ctx.at_turn_limit:
            turn = self._end_turn(
                ctx,
                user_message,
                "We've reached the conversation limit. "
                "Please call the clinic directly for further assistance.",
                ConversationIntent.UNKNOWN,
                "administrative",
            )
            self._log_turn(turn, start_time)
            return turn

        safety = classify_intent(user_message)

        if safety.classification == "urgent_medical":
            turn = self._escalate_turn(ctx, user_message, safety, ESCALATION_URGENT)
            self._log_turn(turn, start_time)
            return turn

        if safety.classification == "medical":
            turn = self._escalate_turn(ctx, user_message, safety, ESCALATION_MEDICAL)
            self._log_turn(turn, start_time)
            return turn

        if ctx.state == ConversationState.AWAITING_CONFIRMATION:
            turn = await self._handle_confirmation(ctx, user_message, actor, safety)
            self._log_turn(turn, start_time)
            return turn

        if ctx.state == ConversationState.GREETING:
            ctx.transition(ConversationState.INTENT_RECOGNITION)

        if ctx.state == ConversationState.INTENT_RECOGNITION:
            ctx.transition(ConversationState.FACT_COLLECTION)

        from fonely.services.conversation_tools import get_business_context

        biz = await get_business_context(business_id, self._session)
        if biz is None:
            turn = self._end_turn(
                ctx,
                user_message,
                "Clinic not found.",
                ConversationIntent.UNKNOWN,
                "administrative",
            )
            self._log_turn(turn, start_time)
            return turn

        self._extract_facts(ctx, user_message, biz)
        self._validate_facts(ctx, biz)
        missing = self._identify_missing_facts(ctx)

        if not missing and ctx.state == ConversationState.FACT_COLLECTION:
            turn = await self._check_availability_and_propose(ctx, user_message, actor, biz, safety)
            self._log_turn(turn, start_time)
            return turn

        response = await self._generate_response(ctx, user_message, biz, missing, safety)
        turn = self._fact_turn(
            ctx,
            user_message,
            sanitize_llm_response(response.text),
            safety,
            missing,
        )
        self._log_turn(turn, start_time)
        return turn

    def _extract_facts(self, ctx: ConversationContext, message: str, biz: object) -> None:
        from fonely.services.conversation_tools import BusinessContext

        assert isinstance(biz, BusinessContext)
        msg_lower = message.lower()

        if "service_id" not in ctx.collected_facts:
            for svc in biz.services:
                if svc.name.lower() in msg_lower:
                    ctx.collected_facts["service_id"] = svc.id
                    ctx.collected_facts["service_name"] = svc.name
                    break

        if "resource_id" not in ctx.collected_facts:
            for res in biz.resources:
                if res.name.lower() in msg_lower:
                    eligible = any(
                        sid == ctx.collected_facts.get("service_id") and rid == res.id
                        for sid, rid in biz.eligibility
                    )
                    if eligible or "service_id" not in ctx.collected_facts:
                        ctx.collected_facts["resource_id"] = res.id
                        ctx.collected_facts["resource_name"] = res.name
                        break

        if "customer_phone" not in ctx.collected_facts:
            phone_match = re.search(r"\+?\d{10,13}", message)
            if phone_match:
                phone = phone_match.group()
                if not phone.startswith("+"):
                    phone = "+91" + phone
                if len(phone) >= 12 and not all(c == "0" for c in phone.lstrip("+")):
                    ctx.collected_facts["customer_phone"] = phone

        if "customer_name" not in ctx.collected_facts:
            name_match = re.search(
                r"(?:my name is|i'?m|name:?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
                message,
                re.IGNORECASE,
            )
            if name_match:
                ctx.collected_facts["customer_name"] = name_match.group(1)

        if "start_at" not in ctx.collected_facts:
            self._extract_datetime(ctx, message)

    def _validate_facts(self, ctx: ConversationContext, biz: object) -> None:
        from fonely.services.conversation_tools import BusinessContext

        assert isinstance(biz, BusinessContext)

        if "start_at" in ctx.collected_facts:
            from datetime import datetime

            start = ctx.collected_facts["start_at"]
            assert isinstance(start, datetime)
            now = utcnow()
            if start <= now:
                del ctx.collected_facts["start_at"]
                return

            hour = start.hour
            weekday = start.isoweekday()
            if weekday == 7:
                del ctx.collected_facts["start_at"]
                return
            in_schedule = (10 <= hour < 13) or (17 <= hour < 21)
            if not in_schedule:
                del ctx.collected_facts["start_at"]
                return

        if "service_id" in ctx.collected_facts and "resource_id" in ctx.collected_facts:
            sid = ctx.collected_facts["service_id"]
            rid = ctx.collected_facts["resource_id"]
            if not any(s == sid and r == rid for s, r in biz.eligibility):
                del ctx.collected_facts["resource_id"]
                ctx.collected_facts.pop("resource_name", None)

    def _extract_datetime(self, ctx: ConversationContext, message: str) -> None:
        msg_lower = message.lower()
        now = utcnow()

        time_match = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", msg_lower)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            ampm = time_match.group(3)
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0

            target_date = now.date()
            if "tomorrow" in msg_lower or "நாளை" in message:
                target_date = (now + timedelta(days=1)).date()

            from datetime import UTC, datetime
            from datetime import time as dt_time

            slot_time = datetime.combine(target_date, dt_time(hour, minute), tzinfo=UTC)
            ctx.collected_facts["start_at"] = slot_time
            return

        if "tomorrow" in msg_lower or "நாளை" in message:
            target_date = (now + timedelta(days=1)).date()
            from datetime import UTC, datetime
            from datetime import time as dt_time

            ctx.collected_facts["start_at"] = datetime.combine(
                target_date, dt_time(10, 0), tzinfo=UTC
            )

    def _identify_missing_facts(self, ctx: ConversationContext) -> list[str]:
        return [f for f in _REQUIRED_FACTS if f not in ctx.collected_facts]

    async def _check_availability_and_propose(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        biz: object,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import (
            BusinessContext,
            check_availability,
            format_confirmation_summary,
        )

        assert isinstance(biz, BusinessContext)
        start_at = ctx.collected_facts["start_at"]
        service_id: int = ctx.collected_facts["service_id"]  # type: ignore[assignment]
        resource_id: int = ctx.collected_facts["resource_id"]  # type: ignore[assignment]

        svc = next((s for s in biz.services if s.id == service_id), None)
        if svc is None:
            return self._fact_turn(
                ctx,
                user_message,
                "Service not found. Please try again.",
                safety,
                ["service_id"],
            )

        ctx.transition(ConversationState.AVAILABILITY_CHECK)

        from datetime import datetime

        assert isinstance(start_at, datetime)
        slots = await check_availability(
            biz.business_id,
            service_id,
            resource_id,
            start_at.date(),
            self._session,
            duration_minutes=svc.duration_minutes,
            buffer_before=svc.buffer_before_minutes,
            buffer_after=svc.buffer_after_minutes,
        )

        if not slots:
            ctx.state = ConversationState.FACT_COLLECTION
            return self._fact_turn(
                ctx,
                user_message,
                "No available slots for that time. Could you try a different date or time?",
                safety,
                ["start_at"],
            )

        if self._appointment_service is not None:
            from fonely.domain.appointments.commands import (
                CreatePendingAppointmentCommand,
            )

            proposal = await self._appointment_service.create_proposal(  # type: ignore[attr-defined]
                CreatePendingAppointmentCommand(
                    actor=actor,
                    service_id=service_id,
                    resource_id=resource_id,
                    start_at=start_at,
                    customer_phone=str(
                        ctx.collected_facts.get("customer_phone", actor.normalized_phone)
                    ),
                    customer_name=ctx.collected_facts.get("customer_name"),  # type: ignore[arg-type]
                    reason=None,
                    call_id=None,
                    expires_at=utcnow() + timedelta(minutes=15),
                    idempotency_key=f"conv-{ctx.conversation_id}",
                )
            )
            ctx.proposal_id = proposal.pending_action_id
            ctx.proposal_version = proposal.version

        ctx.transition(ConversationState.PROPOSAL_PRESENTED)

        resource_name = str(ctx.collected_facts.get("resource_name", ""))
        service_name = str(ctx.collected_facts.get("service_name", ""))
        summary = format_confirmation_summary(
            service_name, resource_name, start_at, svc.price, biz.timezone
        )
        response = f"I've found a slot: {summary}. Shall I book this?"

        ctx.transition(ConversationState.AWAITING_CONFIRMATION)
        return self._fact_turn(ctx, user_message, response, safety, [])

    async def _handle_confirmation(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        decision = detect_confirmation(user_message)

        if decision == "negative":
            ctx.transition(ConversationState.FACT_COLLECTION)
            ctx.collected_facts.pop("start_at", None)
            ctx.proposal_id = None
            ctx.proposal_version = None
            return self._fact_turn(
                ctx,
                user_message,
                "No problem! Would you like a different time?",
                safety,
                self._identify_missing_facts(ctx),
            )

        if decision == "ambiguous":
            return self._fact_turn(
                ctx,
                user_message,
                "Could you confirm — should I go ahead and book this? Please say yes or no.",
                safety,
                [],
            )

        if self._appointment_service is not None and ctx.proposal_id is not None:
            from fonely.domain.appointments.commands import (
                ConfirmPendingAppointmentCommand,
            )
            from fonely.domain.appointments.results import (
                PreCommitAppointmentFailure,
                PreCommitAppointmentSuccess,
            )

            result = await self._appointment_service.confirm_and_commit(  # type: ignore[attr-defined]
                ConfirmPendingAppointmentCommand(
                    actor=actor,
                    pending_action_id=ctx.proposal_id,
                    expected_version=ctx.proposal_version or 1,
                )
            )

            if isinstance(result, PreCommitAppointmentFailure):
                ctx.transition(ConversationState.FACT_COLLECTION)
                ctx.collected_facts.pop("start_at", None)
                ctx.proposal_id = None
                ctx.proposal_version = None
                return self._fact_turn(
                    ctx,
                    user_message,
                    "That slot is no longer available. Would you like to try another time?",
                    safety,
                    ["start_at"],
                )

            assert isinstance(result, PreCommitAppointmentSuccess)
            await self._session.commit()

            ctx.transition(ConversationState.CONFIRMED)
            ctx.transition(ConversationState.COMPLETED)
            return self._fact_turn(
                ctx,
                user_message,
                f"Your appointment is confirmed! "
                f"Appointment ID: {result.appointment.appointment_id}. "
                f"See you at the clinic!",
                safety,
                [],
            )

        ctx.transition(ConversationState.CONFIRMED)
        ctx.transition(ConversationState.COMPLETED)
        return self._fact_turn(
            ctx,
            user_message,
            "Your appointment is confirmed! See you at the clinic!",
            safety,
            [],
        )

    async def _generate_response(
        self,
        ctx: ConversationContext,
        user_message: str,
        biz: object,
        missing: list[str],
        safety: SafetyClassification,
    ) -> ModelResponse:
        from fonely.services.conversation_tools import BusinessContext

        assert isinstance(biz, BusinessContext)
        services_text = ", ".join(
            f"{s.name} (₹{s.price}, {s.duration_minutes}min)" for s in biz.services
        )
        resources_text = ", ".join(r.name for r in biz.resources)

        system_prompt = (
            f"You are the virtual receptionist for {biz.name}. "
            f"You handle appointment bookings and clinic enquiries. "
            f"Respond in the caller's language (Tamil/English/mixed). "
            f"Use short spoken sentences, not written prose. "
            f"Ask one question at a time. Never invent clinic info. "
            f"Available services: {services_text}. "
            f"Available dentists: {resources_text}. "
        )

        if missing:
            system_prompt += (
                f"The customer still needs to provide: "
                f"{', '.join(missing)}. "
                f"Ask about ONE missing item naturally."
            )

        history: list[dict[str, str]] = []
        for t in ctx.turns[-6:]:
            history.append({"role": "user", "content": t.user_message})
            history.append({"role": "assistant", "content": t.assistant_response})
        history.append({"role": "user", "content": user_message})

        return await self._model.complete(
            system_prompt=system_prompt,
            messages=history,
            temperature=0.3,
            max_tokens=300,
        )

    def _log_turn(self, turn: ConversationTurn, start_time: float) -> None:
        latency = round((time.monotonic() - start_time) * 1000)
        logger.info(
            "conversation_turn",
            extra={
                "event": "conversation_turn",
                "conversation_id": turn.conversation_id,
                "turn_id": turn.turn_id,
                "turn_number": len(
                    _CONVERSATIONS.get(
                        turn.conversation_id,
                        ConversationContext(business_id=0),
                    ).turns
                ),
                "state": turn.state.value,
                "intent": turn.intent.value,
                "safety": turn.safety_classification,
                "missing_facts": turn.missing_facts,
                "has_proposal": turn.proposal_id is not None,
                "latency_ms": latency,
            },
        )

    def _fact_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        response: str,
        safety: SafetyClassification,
        missing: list[str],
    ) -> ConversationTurn:
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=response,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=missing,
            proposal_id=ctx.proposal_id,
            proposal_version=ctx.proposal_version,
            intent=safety.intent,
            safety_classification=safety.classification,
        )
        ctx.turns.append(turn)
        return turn

    def _make_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        response: str,
        intent: ConversationIntent,
        classification: str,
    ) -> ConversationTurn:
        return ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=response,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=[],
            intent=intent,
            safety_classification=classification,
        )

    def _escalate_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        safety: SafetyClassification,
        escalation_message: str,
    ) -> ConversationTurn:
        if ctx.can_transition(ConversationState.ESCALATED):
            ctx.transition(ConversationState.ESCALATED)
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=escalation_message,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=[],
            intent=safety.intent,
            safety_classification=safety.classification,
        )
        ctx.turns.append(turn)
        return turn

    def _end_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        response: str,
        intent: ConversationIntent,
        classification: str,
    ) -> ConversationTurn:
        if ctx.can_transition(ConversationState.ENDED):
            ctx.transition(ConversationState.ENDED)
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=response,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=[],
            intent=intent,
            safety_classification=classification,
        )
        ctx.turns.append(turn)
        return turn


def get_conversation(
    conversation_id: str,
) -> ConversationContext | None:
    return _CONVERSATIONS.get(conversation_id)


def create_conversation(business_id: int) -> ConversationContext:
    _evict_stale()
    ctx = ConversationContext(business_id=business_id)
    _CONVERSATIONS[ctx.conversation_id] = ctx
    return ctx
