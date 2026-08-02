"""Unit tests for dental safety boundary."""

from fonely.domain.conversation.safety import classify_intent
from fonely.domain.conversation.state import ConversationIntent


def test_booking_intent() -> None:
    result = classify_intent("I want to book an appointment")
    assert result.intent == ConversationIntent.BOOK_APPOINTMENT
    assert result.classification == "administrative"


def test_availability_check() -> None:
    result = classify_intent("Do you have any available slots tomorrow?")
    assert result.classification == "administrative"


def test_hours_enquiry() -> None:
    result = classify_intent("What are your clinic hours?")
    assert result.classification == "administrative"


def test_fee_enquiry() -> None:
    result = classify_intent("How much does a cleaning cost?")
    assert result.classification == "administrative"


def test_medical_symptom() -> None:
    result = classify_intent("My tooth has been hurting for 3 days")
    assert result.classification == "medical"
    assert result.intent == ConversationIntent.MEDICAL_QUESTION


def test_medical_medication() -> None:
    result = classify_intent("What medicine should I take for the pain?")
    assert result.classification == "medical"


def test_medical_post_op() -> None:
    result = classify_intent("Is swelling normal after extraction?")
    assert result.classification == "medical"


def test_urgent_emergency() -> None:
    result = classify_intent("This is an emergency, heavy bleeding")
    assert result.classification == "urgent_medical"
    assert result.intent == ConversationIntent.URGENT_MEDICAL


def test_urgent_breathing() -> None:
    result = classify_intent("My child can't breathe properly after the procedure")
    assert result.classification == "urgent_medical"


def test_urgent_severe_swelling() -> None:
    result = classify_intent("I have severe swelling and can't open my mouth")
    assert result.classification == "urgent_medical"


def test_unknown_message() -> None:
    result = classify_intent("Hello, how are you?")
    assert result.classification == "administrative"
    assert result.confidence < 0.5


def test_defaults_safe_on_ambiguity() -> None:
    result = classify_intent("xyz")
    assert result.classification == "administrative"


def test_location_enquiry() -> None:
    result = classify_intent("Where is the clinic located? I need directions")
    assert result.classification == "administrative"
