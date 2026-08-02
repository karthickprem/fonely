"""Unit tests for dental safety boundary including Tamil/Tanglish."""

from fonely.domain.conversation.safety import classify_intent, detect_confirmation
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


def test_location_enquiry() -> None:
    result = classify_intent("Where is the clinic located? I need directions")
    assert result.classification == "administrative"


# Tamil/Tanglish safety tests


def test_tamil_tooth_pain() -> None:
    result = classify_intent("பல் வலிக்குது")
    assert result.classification == "medical"


def test_tamil_swelling() -> None:
    result = classify_intent("வீக்கம் இருக்கு")
    assert result.classification == "medical"


def test_tamil_medicine() -> None:
    result = classify_intent("என்ன மருந்து சாப்பிடணும்?")
    assert result.classification == "medical"


def test_tanglish_tooth_pain() -> None:
    result = classify_intent("tooth வலிக்குது romba")
    assert result.classification == "medical"


def test_tamil_urgent_bleeding() -> None:
    result = classify_intent("ரத்தம் நிற்கல, heavy bleeding ஆகுது")
    assert result.classification == "urgent_medical"


def test_tanglish_breathing() -> None:
    result = classify_intent("breathing problem irukku, can't breathe pannala")
    assert result.classification == "urgent_medical"


def test_tamil_booking() -> None:
    result = classify_intent("appointment போடணும்")
    assert result.classification == "administrative"
    assert result.intent == ConversationIntent.BOOK_APPOINTMENT


def test_tanglish_booking() -> None:
    result = classify_intent("book பண்ணணும் oru appointment")
    assert result.classification == "administrative"


def test_tamil_fees() -> None:
    result = classify_intent("fees என்ன? எவ்வளவு ஆகும்?")
    assert result.classification == "administrative"


def test_tamil_availability() -> None:
    result = classify_intent("slot இருக்கா tomorrow?")
    assert result.classification == "administrative"


def test_tanglish_after_extraction() -> None:
    result = classify_intent("extraction-க்கு அப்புறம் pain இருக்கு")
    assert result.classification == "medical"


# Confirmation detection


def test_confirm_positive_english() -> None:
    assert detect_confirmation("yes") == "positive"
    assert detect_confirmation("ok, book it") == "positive"
    assert detect_confirmation("confirm") == "positive"


def test_confirm_positive_tamil() -> None:
    assert detect_confirmation("சரி") == "positive"
    assert detect_confirmation("ஆமா") == "positive"
    assert detect_confirmation("book பண்ணுங்க") == "positive"


def test_confirm_negative_english() -> None:
    assert detect_confirmation("no") == "negative"
    assert detect_confirmation("cancel") == "negative"
    assert detect_confirmation("different time") == "negative"


def test_confirm_negative_tamil() -> None:
    assert detect_confirmation("வேண்டாம்") == "negative"
    assert detect_confirmation("change பண்ணணும்") == "negative"


def test_confirm_ambiguous() -> None:
    assert detect_confirmation("hmm let me think") == "ambiguous"
    assert detect_confirmation("I'm not sure") == "ambiguous"
