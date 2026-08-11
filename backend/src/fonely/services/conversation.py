"""Conversation orchestrator for dental appointment booking."""

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timedelta

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
        for key, indexed_id in list(_PHONE_INDEX.items()):
            if indexed_id == cid:
                _PHONE_INDEX.pop(key, None)
    if stale:
        logger.info("conversations_evicted", extra={"count": len(stale)})

    if len(_CONVERSATIONS) >= _MAX_CONVERSATIONS:
        sorted_convs = sorted(_CONVERSATIONS.items(), key=lambda x: x[1].created_at)
        to_remove = len(_CONVERSATIONS) - _MAX_CONVERSATIONS + 1
        for cid, _ in sorted_convs[:to_remove]:
            del _CONVERSATIONS[cid]
            _CONVERSATION_LOCKS.pop(cid, None)
        logger.info("conversations_evicted_capacity", extra={"count": to_remove})


def invalidate_conversation_cache(business_id: int, phone: str) -> None:
    """Remove cached conversation state for a tenant+phone after rollback."""
    key = (business_id, phone)
    conv_id = _PHONE_INDEX.pop(key, None)
    if conv_id:
        _CONVERSATIONS.pop(conv_id, None)
        _CONVERSATION_LOCKS.pop(conv_id, None)


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
        appointment_service: object,
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
        from fonely.core.pii_audit import log_pii_access

        log_pii_access(
            operation="read",
            data_type="conversation",
            business_id=business_id,
            accessor="service:conversation",
            record_count=1,
        )
        lock = _get_lock(conversation_id)
        async with lock:
            try:
                async with asyncio.timeout(settings.conversation_timeout_seconds):
                    turn = await self._process_inner(
                        conversation_id, business_id, actor, user_message
                    )
                    await self._persist_turn(conversation_id, turn)
                    return turn
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
            try:
                from fonely.services.conversation_persistence import (
                    ConversationPersistenceService,
                )

                persistence = ConversationPersistenceService(self._session)
                loaded = await persistence.load_by_id(conversation_id)
                if loaded is not None:
                    ctx = loaded
                    _CONVERSATIONS[conversation_id] = ctx
            except Exception:
                logger.debug("db_conversation_load_skipped", exc_info=True)
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

        if ctx.state == ConversationState.CANCEL_SELECTION:
            turn = await self._handle_cancel_selection(ctx, user_message, actor, safety)
            self._log_turn(turn, start_time)
            return turn

        if ctx.state == ConversationState.RESCHEDULE_SELECTION:
            turn = await self._handle_reschedule_selection(ctx, user_message, actor, safety)
            self._log_turn(turn, start_time)
            return turn

        if ctx.state == ConversationState.GREETING:
            ctx.transition(ConversationState.INTENT_RECOGNITION)

        if ctx.state == ConversationState.INTENT_RECOGNITION:
            if safety.intent == ConversationIntent.CANCEL_APPOINTMENT:
                turn = await self._handle_cancel_intent(ctx, user_message, actor, safety)
                self._log_turn(turn, start_time)
                return turn
            if safety.intent == ConversationIntent.RESCHEDULE:
                turn = await self._handle_reschedule_intent(ctx, user_message, actor, safety)
                self._log_turn(turn, start_time)
                return turn
            ctx.transition(ConversationState.FACT_COLLECTION)
            ctx.collected_facts["_operation"] = "book"

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

        await self._extract_facts(ctx, user_message, biz)
        await self._validate_facts(ctx, biz)
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

    @staticmethod
    def _invalidate_offer_if_changed(
        ctx: ConversationContext, fact_key: str, new_value: object
    ) -> None:
        old = ctx.collected_facts.get(fact_key)
        if old is not None and old != new_value:
            ctx.collected_facts.pop("_active_offer", None)

    async def _extract_facts(self, ctx: ConversationContext, message: str, biz: object) -> None:
        from fonely.services.conversation_tools import BusinessContext

        assert isinstance(biz, BusinessContext)
        msg_lower = message.lower()

        regex_found = False

        for svc in biz.services:
            if svc.name.lower() in msg_lower:
                if ctx.collected_facts.get("service_id") != svc.id:
                    self._invalidate_offer_if_changed(ctx, "service_id", svc.id)
                    ctx.collected_facts["service_id"] = svc.id
                    ctx.collected_facts["service_name"] = svc.name
                    regex_found = True
                break

        for res in biz.resources:
            if res.name.lower() in msg_lower:
                eligible = any(
                    sid == ctx.collected_facts.get("service_id") and rid == res.id
                    for sid, rid in biz.eligibility
                )
                if (
                    eligible or "service_id" not in ctx.collected_facts
                ) and ctx.collected_facts.get("resource_id") != res.id:
                    self._invalidate_offer_if_changed(ctx, "resource_id", res.id)
                    ctx.collected_facts["resource_id"] = res.id
                    ctx.collected_facts["resource_name"] = res.name
                    regex_found = True
                break

        if "customer_phone" not in ctx.collected_facts:
            phone_match = re.search(r"\+?\d{10,13}", message)
            if phone_match:
                phone = phone_match.group()
                if not phone.startswith("+"):
                    phone = "+91" + phone
                if len(phone) >= 12 and not all(c == "0" for c in phone.lstrip("+")):
                    ctx.collected_facts["customer_phone"] = phone
                    regex_found = True

        if "customer_name" not in ctx.collected_facts:
            name_match = re.search(
                r"(?:my name is|i'?m|name:?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
                message,
                re.IGNORECASE,
            )
            if name_match:
                ctx.collected_facts["customer_name"] = name_match.group(1)
                regex_found = True

        # Always attempt datetime extraction so time corrections and offered-slot
        # selections are honored even after start_at is first set.
        had_start = "start_at" in ctx.collected_facts
        self._extract_datetime(ctx, message, biz.timezone)
        if not had_start and "start_at" in ctx.collected_facts:
            regex_found = True

        if not regex_found:
            try:
                from fonely.services.fact_extractor import FactExtractor
                from fonely.services.fact_resolver import FactResolver

                extractor = FactExtractor(self._model)
                extracted = await extractor.extract(message, biz, ctx.collected_facts)
                resolved = FactResolver().resolve(extracted, biz, biz.timezone)
                for key, value in resolved.to_dict().items():
                    if key not in ctx.collected_facts:
                        ctx.collected_facts[key] = value
            except Exception:
                logger.warning("llm_fact_extraction_failed", exc_info=True)

    async def _validate_facts(self, ctx: ConversationContext, biz: object) -> None:
        from fonely.services.conversation_tools import BusinessContext

        assert isinstance(biz, BusinessContext)

        if "start_at" in ctx.collected_facts:
            from datetime import datetime

            start = ctx.collected_facts["start_at"]
            assert isinstance(start, datetime)
            if start <= utcnow():
                del ctx.collected_facts["start_at"]
                return

        if "service_id" in ctx.collected_facts and "resource_id" in ctx.collected_facts:
            sid = ctx.collected_facts["service_id"]
            rid = ctx.collected_facts["resource_id"]
            if not any(s == sid and r == rid for s, r in biz.eligibility):
                del ctx.collected_facts["resource_id"]
                ctx.collected_facts.pop("resource_name", None)

    def _try_offer_selection(self, ctx: ConversationContext, message: str) -> bool:
        offer_data = ctx.collected_facts.get("_active_offer")
        if not offer_data or not isinstance(offer_data, dict):
            return False

        from fonely.domain.booking.offers import (
            OfferValidationError,
            deserialize_offer,
            validate_selection,
        )

        try:
            offer = deserialize_offer(offer_data)
        except OfferValidationError:
            ctx.collected_facts.pop("_active_offer", None)
            return False
        if offer is None:
            ctx.collected_facts.pop("_active_offer", None)
            return False

        matched_slot = None

        # 1. Parse time from message and match against offered slot times
        time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", message, re.IGNORECASE)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or "0")
            ampm = time_match.group(3).lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            for slot in offer.slots:
                slot_match = re.match(
                    r"(\d{1,2}):(\d{2})\s*(am|pm)",
                    slot.display_time.lower().strip(),
                )
                if slot_match:
                    sh = int(slot_match.group(1))
                    sm = int(slot_match.group(2))
                    sa = slot_match.group(3)
                    if sa == "pm" and sh < 12:
                        sh += 12
                    elif sa == "am" and sh == 12:
                        sh = 0
                    if sh == hour and sm == minute:
                        matched_slot = slot
                        break

        # 2. Word-boundary ordinal matching (only if no time match)
        if matched_slot is None:
            msg_lower = message.strip().lower()
            _ordinals = [
                (r"\bfirst\b", 0),
                (r"\bsecond\b", 1),
                (r"\bthird\b", 2),
            ]
            for pattern, idx in _ordinals:
                if re.search(pattern, msg_lower) and idx < len(offer.slots):
                    matched_slot = offer.slots[idx]
                    break

        if matched_slot is None:
            return False

        # B3 fix: use trusted context ids, not the stored offer's own ids
        try:
            selected = validate_selection(
                offer,
                matched_slot.token,
                business_id=ctx.business_id,
                conversation_id=ctx.conversation_id,
            )
        except OfferValidationError:
            ctx.collected_facts.pop("_active_offer", None)
            return False

        ctx.collected_facts["start_at"] = selected.start_at_utc
        ctx.collected_facts["_selected_token"] = selected.token
        ctx.collected_facts["_selected_offer_id"] = selected.offer_id
        return True

    def _extract_datetime(
        self, ctx: ConversationContext, message: str, timezone: str = "Asia/Kolkata"
    ) -> None:
        from datetime import UTC, datetime
        from datetime import time as dt_time
        from zoneinfo import ZoneInfo

        if self._try_offer_selection(ctx, message):
            return

        msg_lower = message.lower()
        clinic_tz = ZoneInfo(timezone)
        now = datetime.now(clinic_tz)

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

            local_dt = datetime.combine(target_date, dt_time(hour, minute), tzinfo=clinic_tz)
            new_start = local_dt.astimezone(UTC)
            # A raw time reaching here is not an offer selection (that was tried
            # first), so any active offer is now stale — drop it.
            if ctx.collected_facts.get("start_at") != new_start:
                ctx.collected_facts.pop("_active_offer", None)
            ctx.collected_facts["start_at"] = new_start
            return

        if "tomorrow" in msg_lower or "நாளை" in message:
            target_date = (now + timedelta(days=1)).date()
            local_dt = datetime.combine(target_date, dt_time(10, 0), tzinfo=clinic_tz)
            new_start = local_dt.astimezone(UTC)
            if ctx.collected_facts.get("start_at") != new_start:
                ctx.collected_facts.pop("_active_offer", None)
            ctx.collected_facts["start_at"] = new_start

    def _identify_missing_facts(self, ctx: ConversationContext) -> list[str]:
        operation = ctx.collected_facts.get("_operation", "book")
        if operation == "cancel":
            return []
        if operation == "reschedule":
            return [f for f in ("start_at",) if f not in ctx.collected_facts]
        return [f for f in _REQUIRED_FACTS if f not in ctx.collected_facts]

    async def _handle_cancel_intent(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import (
            format_appointment_list,
            get_business_context,
            get_patient_appointments,
        )

        ctx.transition(ConversationState.CANCEL_SELECTION)
        ctx.collected_facts["_operation"] = "cancel"

        biz = await get_business_context(actor.business_id, self._session)
        timezone = biz.timezone if biz else "Asia/Kolkata"
        ctx.collected_facts["_business_timezone"] = timezone

        appointments = await get_patient_appointments(
            actor.business_id, actor.normalized_phone, self._session
        )

        if not appointments:
            ctx.transition(ConversationState.ENDED)
            return self._fact_turn(
                ctx, user_message, "You don't have any upcoming appointments.", safety, []
            )

        if len(appointments) == 1:
            appt = appointments[0]
            return await self._create_cancel_proposal(
                ctx, user_message, actor, safety, appt, timezone
            )

        ctx.collected_facts["_candidates"] = [
            {
                "appointment_id": a.appointment_id,
                "service_name": a.service_name,
                "resource_name": a.resource_name,
                "start_at": a.start_at.isoformat(),
                "version": a.version,
                "pending_action_id": a.pending_action_id,
                "service_id": a.service_id,
                "resource_id": a.resource_id,
                "price": a.price,
                "status": a.status,
            }
            for a in appointments
        ]
        listing = format_appointment_list(appointments, timezone)
        return self._fact_turn(
            ctx,
            user_message,
            f"Which appointment would you like to cancel?\n{listing}",
            safety,
            [],
        )

    async def _handle_cancel_selection(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import (
            PatientAppointment,
            parse_appointment_selection,
        )

        candidates_raw = ctx.collected_facts.get("_candidates", [])
        assert isinstance(candidates_raw, list)
        candidates = [
            PatientAppointment(
                appointment_id=c["appointment_id"],
                service_name=c["service_name"],
                resource_name=c["resource_name"],
                start_at=datetime.fromisoformat(c["start_at"]),
                price=c.get("price"),
                status=c["status"],
                pending_action_id=c["pending_action_id"],
                version=c["version"],
                service_id=c["service_id"],
                resource_id=c["resource_id"],
            )
            for c in candidates_raw
        ]

        selected = parse_appointment_selection(user_message, candidates)
        if selected is None:
            return self._fact_turn(
                ctx,
                user_message,
                "I didn't understand. Please reply with the number of the appointment.",
                safety,
                [],
            )

        timezone = str(ctx.collected_facts.get("_business_timezone", "Asia/Kolkata"))
        return await self._create_cancel_proposal(
            ctx, user_message, actor, safety, selected, timezone
        )

    async def _create_cancel_proposal(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
        appt: object,
        timezone: str,
    ) -> ConversationTurn:
        from fonely.domain.appointments.commands import (
            CreatePendingAppointmentCancellationCommand,
        )
        from fonely.services.conversation_tools import (
            PatientAppointment,
            format_confirmation_summary,
        )

        assert isinstance(appt, PatientAppointment)

        proposal = await self._appointment_service.create_cancellation_proposal(  # type: ignore[attr-defined]
            CreatePendingAppointmentCancellationCommand(
                actor=actor,
                appointment_id=appt.appointment_id,
                expected_appointment_version=appt.version,
                reason_code=None,
                expires_at=utcnow() + timedelta(minutes=15),
                idempotency_key=f"conv-{ctx.conversation_id}-cancel-{appt.appointment_id}",
            )
        )
        ctx.proposal_id = proposal.pending_action_id
        ctx.proposal_version = proposal.version
        ctx.collected_facts["_target_appointment_id"] = appt.appointment_id

        summary = format_confirmation_summary(
            appt.service_name, appt.resource_name, appt.start_at, appt.price, timezone
        )
        ctx.transition(ConversationState.AWAITING_CONFIRMATION)
        return self._fact_turn(
            ctx,
            user_message,
            f"Cancel your {summary}? Say yes to confirm.",
            safety,
            [],
        )

    async def _handle_reschedule_intent(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import (
            format_appointment_list,
            get_business_context,
            get_patient_appointments,
        )

        ctx.transition(ConversationState.RESCHEDULE_SELECTION)
        ctx.collected_facts["_operation"] = "reschedule"

        biz = await get_business_context(actor.business_id, self._session)
        timezone = biz.timezone if biz else "Asia/Kolkata"
        ctx.collected_facts["_business_timezone"] = timezone

        appointments = await get_patient_appointments(
            actor.business_id, actor.normalized_phone, self._session
        )

        if not appointments:
            ctx.transition(ConversationState.ENDED)
            return self._fact_turn(
                ctx, user_message, "You don't have any upcoming appointments.", safety, []
            )

        if len(appointments) == 1:
            appt = appointments[0]
            return self._select_reschedule_appointment(ctx, user_message, safety, appt, timezone)

        ctx.collected_facts["_candidates"] = [
            {
                "appointment_id": a.appointment_id,
                "service_name": a.service_name,
                "resource_name": a.resource_name,
                "start_at": a.start_at.isoformat(),
                "version": a.version,
                "pending_action_id": a.pending_action_id,
                "service_id": a.service_id,
                "resource_id": a.resource_id,
                "price": a.price,
                "status": a.status,
            }
            for a in appointments
        ]
        listing = format_appointment_list(appointments, timezone)
        return self._fact_turn(
            ctx,
            user_message,
            f"Which appointment would you like to reschedule?\n{listing}",
            safety,
            [],
        )

    async def _handle_reschedule_selection(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import (
            PatientAppointment,
            parse_appointment_selection,
        )

        candidates_raw = ctx.collected_facts.get("_candidates", [])
        assert isinstance(candidates_raw, list)
        candidates = [
            PatientAppointment(
                appointment_id=c["appointment_id"],
                service_name=c["service_name"],
                resource_name=c["resource_name"],
                start_at=datetime.fromisoformat(c["start_at"]),
                price=c.get("price"),
                status=c["status"],
                pending_action_id=c["pending_action_id"],
                version=c["version"],
                service_id=c["service_id"],
                resource_id=c["resource_id"],
            )
            for c in candidates_raw
        ]

        selected = parse_appointment_selection(user_message, candidates)
        if selected is None:
            return self._fact_turn(
                ctx,
                user_message,
                "I didn't understand. Please reply with the number of the appointment.",
                safety,
                [],
            )

        timezone = str(ctx.collected_facts.get("_business_timezone", "Asia/Kolkata"))
        return self._select_reschedule_appointment(ctx, user_message, safety, selected, timezone)

    def _select_reschedule_appointment(
        self,
        ctx: ConversationContext,
        user_message: str,
        safety: SafetyClassification,
        appt: object,
        timezone: str,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import PatientAppointment

        assert isinstance(appt, PatientAppointment)
        ctx.collected_facts["_target_appointment_id"] = appt.appointment_id
        ctx.collected_facts["_target_appointment_version"] = appt.version
        ctx.collected_facts["service_id"] = appt.service_id
        ctx.collected_facts["service_name"] = appt.service_name
        ctx.collected_facts["resource_id"] = appt.resource_id
        ctx.collected_facts["resource_name"] = appt.resource_name
        ctx.collected_facts["customer_phone"] = str(ctx.collected_facts.get("customer_phone", ""))

        ctx.transition(ConversationState.FACT_COLLECTION)
        return self._fact_turn(
            ctx,
            user_message,
            "When would you like to reschedule to? Please tell me the new date and time.",
            safety,
            ["start_at"],
        )

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

        from fonely.domain.booking.orchestrator import BookingOrchestrator

        assert isinstance(start_at, datetime)
        operation = ctx.collected_facts.get("_operation", "book")
        exclude_appointment_id: int | None = None
        if operation == "reschedule":
            target_id = ctx.collected_facts.get("_target_appointment_id")
            assert isinstance(target_id, int)
            exclude_appointment_id = target_id

        res = next((r for r in biz.resources if r.id == resource_id), None)
        resource_name = res.name if res else "Doctor"

        orchestrator = BookingOrchestrator(self._session)
        exact_available, offer = await orchestrator.check_and_offer(
            business_id=biz.business_id,
            conversation_id=ctx.conversation_id,
            service_id=service_id,
            service_name=svc.name,
            resource_id=resource_id,
            resource_name=resource_name,
            requested_start=start_at,
            business_timezone=biz.timezone,
            exclude_appointment_id=exclude_appointment_id,
        )

        if not exact_available:
            ctx.state = ConversationState.FACT_COLLECTION
            ctx.booking_attempt += 1
            del ctx.collected_facts["start_at"]
            if offer and offer.slots:
                ctx.collected_facts["_active_offer"] = orchestrator.serialize(offer)
                alt_texts = [s.display_time for s in offer.slots]
                response = (
                    "That exact time isn't available. Nearest slots: "
                    f"{', '.join(alt_texts)}. Which one works?"
                )
            else:
                ctx.collected_facts.pop("_active_offer", None)
                response = "That time isn't available. Would you like to try another date?"
            return self._fact_turn(
                ctx,
                user_message,
                response,
                safety,
                ["start_at"],
            )

        if offer:
            ctx.collected_facts["_active_offer"] = orchestrator.serialize(offer)

        operation = ctx.collected_facts.get("_operation", "book")

        if operation == "reschedule":
            from fonely.domain.appointments.commands import (
                CreatePendingAppointmentRescheduleCommand,
            )

            target_appt_id: int = ctx.collected_facts["_target_appointment_id"]  # type: ignore[assignment]
            target_appt_version: int = ctx.collected_facts["_target_appointment_version"]  # type: ignore[assignment]
            proposal = await self._appointment_service.create_reschedule_proposal(  # type: ignore[attr-defined]
                CreatePendingAppointmentRescheduleCommand(
                    actor=actor,
                    appointment_id=target_appt_id,
                    expected_appointment_version=target_appt_version,
                    service_id=service_id,
                    resource_id=resource_id,
                    start_at=start_at,
                    expires_at=utcnow() + timedelta(minutes=15),
                    idempotency_key=f"conv-{ctx.conversation_id}-reschedule-{target_appt_id}-a{ctx.booking_attempt}",
                )
            )
        else:
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
                    idempotency_key=f"conv-{ctx.conversation_id}-a{ctx.booking_attempt}",
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
        if operation == "reschedule":
            response = f"Move your appointment to {summary}? Say yes to confirm."
        else:
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
        operation = ctx.collected_facts.get("_operation", "book")
        decision = detect_confirmation(user_message)

        if decision == "negative":
            if operation == "cancel":
                ctx.transition(ConversationState.ENDED)
                return self._fact_turn(
                    ctx, user_message, "Okay, your appointment is unchanged.", safety, []
                )
            ctx.transition(ConversationState.FACT_COLLECTION)
            ctx.booking_attempt += 1
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
            action_word = {"cancel": "cancel", "reschedule": "reschedule"}.get(
                str(operation), "book"
            )
            return self._fact_turn(
                ctx,
                user_message,
                f"Could you confirm — should I go ahead and {action_word} this? "
                "Please say yes or no.",
                safety,
                [],
            )

        if ctx.proposal_id is None:
            return self._fact_turn(
                ctx,
                user_message,
                "Something went wrong. Let's start over.",
                safety,
                self._identify_missing_facts(ctx),
            )

        if operation == "cancel":
            return await self._confirm_cancellation(ctx, user_message, actor, safety)
        if operation == "reschedule":
            return await self._confirm_reschedule(ctx, user_message, actor, safety)
        return await self._confirm_booking(ctx, user_message, actor, safety)

    async def _confirm_booking(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.domain.appointments.commands import ConfirmPendingAppointmentCommand
        from fonely.domain.appointments.errors import AppointmentDomainError
        from fonely.domain.appointments.results import (
            PreCommitAppointmentFailure,
            PreCommitAppointmentSuccess,
        )

        assert ctx.proposal_id is not None
        try:
            result = await self._appointment_service.confirm_and_commit(  # type: ignore[attr-defined]
                ConfirmPendingAppointmentCommand(
                    actor=actor,
                    pending_action_id=ctx.proposal_id,
                    expected_version=ctx.proposal_version or 1,
                )
            )
        except (AppointmentDomainError, ValueError):
            ctx.state = ConversationState.FACT_COLLECTION
            ctx.booking_attempt += 1
            ctx.collected_facts.pop("start_at", None)
            ctx.proposal_id = None
            ctx.proposal_version = None
            return self._fact_turn(
                ctx,
                user_message,
                "That time is no longer available. Would you like to try another time?",
                safety,
                ["start_at"],
            )

        if isinstance(result, PreCommitAppointmentFailure):
            ctx.state = ConversationState.FACT_COLLECTION
            ctx.booking_attempt += 1
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

    async def _confirm_cancellation(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.domain.appointments.commands import (
            ConfirmPendingAppointmentCancellationCommand,
        )

        assert ctx.proposal_id is not None
        await self._appointment_service.confirm_cancellation(  # type: ignore[attr-defined]
            ConfirmPendingAppointmentCancellationCommand(
                actor=actor,
                pending_action_id=ctx.proposal_id,
                expected_version=ctx.proposal_version or 1,
            )
        )

        ctx.transition(ConversationState.CONFIRMED)
        ctx.transition(ConversationState.COMPLETED)
        return self._fact_turn(
            ctx,
            user_message,
            "Your appointment has been cancelled. The clinic has been notified.",
            safety,
            [],
        )

    async def _confirm_reschedule(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.domain.appointments.commands import (
            ConfirmPendingAppointmentRescheduleCommand,
        )
        from fonely.domain.appointments.errors import AppointmentDomainError

        assert ctx.proposal_id is not None
        try:
            result = await self._appointment_service.confirm_reschedule(  # type: ignore[attr-defined]
                ConfirmPendingAppointmentRescheduleCommand(
                    actor=actor,
                    pending_action_id=ctx.proposal_id,
                    expected_version=ctx.proposal_version or 1,
                )
            )
        except (AppointmentDomainError, ValueError):
            ctx.state = ConversationState.FACT_COLLECTION
            ctx.booking_attempt += 1
            ctx.collected_facts.pop("start_at", None)
            ctx.proposal_id = None
            ctx.proposal_version = None
            return self._fact_turn(
                ctx,
                user_message,
                "That slot isn't available. Would you like to try another time?",
                safety,
                ["start_at"],
            )

        ctx.transition(ConversationState.CONFIRMED)
        ctx.transition(ConversationState.COMPLETED)

        from fonely.services.conversation_tools import format_confirmation_summary

        new_start = result.start_at
        resource_name = str(result.resource_name)
        service_name = str(ctx.collected_facts.get("service_name", ""))
        biz_tz = str(ctx.collected_facts.get("_business_timezone", "Asia/Kolkata"))
        summary = format_confirmation_summary(service_name, resource_name, new_start, None, biz_tz)
        return self._fact_turn(
            ctx,
            user_message,
            f"Your appointment has been rescheduled to {summary}.",
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

    async def _persist_turn(self, conversation_id: str, turn: ConversationTurn) -> None:
        from fonely.services.conversation_persistence import (
            ConversationPersistenceService,
        )

        ctx = _CONVERSATIONS.get(conversation_id)
        if ctx is None:
            return

        has_critical_state = ctx.proposal_id is not None or turn.state in (
            ConversationState.CONFIRMED,
            ConversationState.COMPLETED,
        )

        try:
            persistence = ConversationPersistenceService(self._session)
            exists = await persistence.exists(conversation_id)
            if not exists:
                if has_critical_state:
                    from fonely.core.metrics import metrics

                    metrics.increment(
                        "conversation_critical_state_unpersisted",
                        {"business_id": str(ctx.business_id)},
                    )
                    logger.warning(
                        "critical_state_not_persisted: conversation=%s",
                        conversation_id,
                    )
                return
            async with self._session.begin_nested():
                await persistence.save_turn(ctx, turn)
        except Exception:
            if has_critical_state:
                _CONVERSATIONS.pop(conversation_id, None)
                raise
            logger.debug("conversation_persist_skipped", exc_info=True)

    def _log_turn(self, turn: ConversationTurn, start_time: float) -> None:
        latency = round((time.monotonic() - start_time) * 1000)

        from fonely.core.metrics import metrics

        bid = str(turn.business_id)
        metrics.increment(
            "conversation_turns_total",
            {"business_id": bid, "state": turn.state.value, "intent": turn.intent.value},
        )
        metrics.observe("conversation_turn_duration_ms", latency, {"business_id": bid})
        if turn.safety_classification != "administrative":
            metrics.increment(
                "safety_classifications_total",
                {"classification": turn.safety_classification},
            )

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


_PHONE_INDEX: dict[tuple[int, str], str] = {}


def find_or_create_conversation(
    business_id: int,
    customer_phone: str,
) -> ConversationContext:
    key = (business_id, customer_phone)
    existing_id = _PHONE_INDEX.get(key)
    if existing_id is not None:
        ctx = _CONVERSATIONS.get(existing_id)
        if ctx is not None and ctx.state not in (
            ConversationState.COMPLETED,
            ConversationState.ENDED,
        ):
            return ctx

    ctx = create_conversation(business_id)
    _PHONE_INDEX[key] = ctx.conversation_id
    return ctx


async def find_or_create_conversation_persistent(
    business_id: int,
    customer_phone: str,
    session: object,
) -> ConversationContext:
    key = (business_id, customer_phone)
    existing_id = _PHONE_INDEX.get(key)
    if existing_id is not None:
        ctx = _CONVERSATIONS.get(existing_id)
        if ctx is not None and ctx.state not in (
            ConversationState.COMPLETED,
            ConversationState.ENDED,
        ):
            return ctx

    try:
        from fonely.services.conversation_persistence import ConversationPersistenceService

        persistence = ConversationPersistenceService(session)  # type: ignore[arg-type]
        ctx = await persistence.load_or_create(business_id, customer_phone)
        _CONVERSATIONS[ctx.conversation_id] = ctx
        _PHONE_INDEX[key] = ctx.conversation_id
        return ctx
    except Exception:
        logger.debug("persistent_find_or_create_failed", exc_info=True)
        return find_or_create_conversation(business_id, customer_phone)
