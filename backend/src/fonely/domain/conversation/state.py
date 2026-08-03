"""Conversation state machine for appointment booking."""

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from fonely.core.validators import utcnow


class ConversationState(enum.StrEnum):
    GREETING = "greeting"
    INTENT_RECOGNITION = "intent_recognition"
    FACT_COLLECTION = "fact_collection"
    AVAILABILITY_CHECK = "availability_check"
    PROPOSAL_PRESENTED = "proposal_presented"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    ENDED = "ended"
    CANCEL_SELECTION = "cancel_selection"
    RESCHEDULE_SELECTION = "reschedule_selection"


class ConversationIntent(enum.StrEnum):
    BOOK_APPOINTMENT = "book_appointment"
    CHECK_AVAILABILITY = "check_availability"
    ASK_HOURS = "ask_hours"
    ASK_SERVICES = "ask_services"
    ASK_FEES = "ask_fees"
    CANCEL_APPOINTMENT = "cancel_appointment"
    RESCHEDULE = "reschedule"
    GENERAL_ENQUIRY = "general_enquiry"
    MEDICAL_QUESTION = "medical_question"
    URGENT_MEDICAL = "urgent_medical"
    UNKNOWN = "unknown"


_VALID_TRANSITIONS: dict[ConversationState, frozenset[ConversationState]] = {
    ConversationState.GREETING: frozenset(
        {ConversationState.INTENT_RECOGNITION, ConversationState.ESCALATED, ConversationState.ENDED}
    ),
    ConversationState.INTENT_RECOGNITION: frozenset(
        {
            ConversationState.FACT_COLLECTION,
            ConversationState.CANCEL_SELECTION,
            ConversationState.RESCHEDULE_SELECTION,
            ConversationState.ESCALATED,
            ConversationState.ENDED,
        }
    ),
    ConversationState.FACT_COLLECTION: frozenset(
        {
            ConversationState.AVAILABILITY_CHECK,
            ConversationState.ESCALATED,
            ConversationState.ENDED,
        }
    ),
    ConversationState.AVAILABILITY_CHECK: frozenset(
        {
            ConversationState.PROPOSAL_PRESENTED,
            ConversationState.FACT_COLLECTION,
            ConversationState.ESCALATED,
            ConversationState.ENDED,
        }
    ),
    ConversationState.PROPOSAL_PRESENTED: frozenset(
        {
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.FACT_COLLECTION,
            ConversationState.ESCALATED,
            ConversationState.ENDED,
        }
    ),
    ConversationState.AWAITING_CONFIRMATION: frozenset(
        {
            ConversationState.CONFIRMED,
            ConversationState.FACT_COLLECTION,
            ConversationState.ESCALATED,
            ConversationState.ENDED,
        }
    ),
    ConversationState.CONFIRMED: frozenset({ConversationState.COMPLETED, ConversationState.ENDED}),
    ConversationState.COMPLETED: frozenset(),
    ConversationState.ESCALATED: frozenset({ConversationState.ENDED}),
    ConversationState.ENDED: frozenset(),
    ConversationState.CANCEL_SELECTION: frozenset(
        {
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.ENDED,
        }
    ),
    ConversationState.RESCHEDULE_SELECTION: frozenset(
        {
            ConversationState.FACT_COLLECTION,
            ConversationState.ENDED,
        }
    ),
}

MAX_TURNS = 20


@dataclass
class ConversationTurn:
    turn_id: str
    conversation_id: str
    business_id: int
    state: ConversationState
    user_message: str
    assistant_response: str
    collected_facts: dict[str, object]
    missing_facts: list[str]
    proposal_id: int | None = None
    proposal_version: int | None = None
    intent: ConversationIntent = ConversationIntent.UNKNOWN
    safety_classification: str = "administrative"
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class ConversationContext:
    conversation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    business_id: int = 0
    state: ConversationState = ConversationState.GREETING
    turns: list[ConversationTurn] = field(default_factory=list)
    collected_facts: dict[str, object] = field(default_factory=dict)
    proposal_id: int | None = None
    proposal_version: int | None = None
    created_at: datetime = field(default_factory=utcnow)

    def can_transition(self, target: ConversationState) -> bool:
        return target in _VALID_TRANSITIONS.get(self.state, frozenset())

    def transition(self, target: ConversationState) -> None:
        if not self.can_transition(target):
            raise ValueError(f"Invalid transition: {self.state} → {target}")
        self.state = target

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def at_turn_limit(self) -> bool:
        return self.turn_count >= MAX_TURNS
