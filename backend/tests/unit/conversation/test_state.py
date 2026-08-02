"""Unit tests for conversation state machine."""

import pytest

from fonely.domain.conversation.state import (
    MAX_TURNS,
    ConversationContext,
    ConversationState,
    ConversationTurn,
)


def test_initial_state_is_greeting() -> None:
    ctx = ConversationContext(business_id=1)
    assert ctx.state == ConversationState.GREETING


def test_valid_greeting_to_intent() -> None:
    ctx = ConversationContext(business_id=1)
    ctx.transition(ConversationState.INTENT_RECOGNITION)
    assert ctx.state == ConversationState.INTENT_RECOGNITION


def test_valid_intent_to_fact_collection() -> None:
    ctx = ConversationContext(business_id=1)
    ctx.transition(ConversationState.INTENT_RECOGNITION)
    ctx.transition(ConversationState.FACT_COLLECTION)
    assert ctx.state == ConversationState.FACT_COLLECTION


def test_invalid_greeting_to_confirmed_raises() -> None:
    ctx = ConversationContext(business_id=1)
    with pytest.raises(ValueError, match="Invalid transition"):
        ctx.transition(ConversationState.CONFIRMED)


def test_completed_has_no_transitions() -> None:
    ctx = ConversationContext(business_id=1)
    ctx.state = ConversationState.COMPLETED
    assert not ctx.can_transition(ConversationState.GREETING)
    assert not ctx.can_transition(ConversationState.ENDED)


def test_any_state_can_escalate() -> None:
    for state in [
        ConversationState.GREETING,
        ConversationState.INTENT_RECOGNITION,
        ConversationState.FACT_COLLECTION,
        ConversationState.PROPOSAL_PRESENTED,
        ConversationState.AWAITING_CONFIRMATION,
    ]:
        ctx = ConversationContext(business_id=1)
        ctx.state = state
        assert ctx.can_transition(ConversationState.ESCALATED)


def test_confirmation_requires_prior_proposal() -> None:
    ctx = ConversationContext(business_id=1)
    assert not ctx.can_transition(ConversationState.CONFIRMED)
    ctx.state = ConversationState.AWAITING_CONFIRMATION
    assert ctx.can_transition(ConversationState.CONFIRMED)


def test_fact_collection_after_proposal_change() -> None:
    ctx = ConversationContext(business_id=1)
    ctx.state = ConversationState.AWAITING_CONFIRMATION
    ctx.transition(ConversationState.FACT_COLLECTION)
    assert ctx.state == ConversationState.FACT_COLLECTION


def test_turn_limit() -> None:
    ctx = ConversationContext(business_id=1)
    assert not ctx.at_turn_limit
    for i in range(MAX_TURNS):
        ctx.turns.append(
            ConversationTurn(
                turn_id=str(i),
                conversation_id=ctx.conversation_id,
                business_id=1,
                state=ctx.state,
                user_message="test",
                assistant_response="test",
                collected_facts={},
                missing_facts=[],
            )
        )
    assert ctx.at_turn_limit
    assert ctx.turn_count == MAX_TURNS
