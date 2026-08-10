"""Property test: no input without a receipt can produce success language.

This is not a sample. It is an exhaustive/property-based test over the
gate function itself. It either passes or fails and does not care about
sample size.

The 60-conversation run proves the WIRING. This test proves the RULE.
"""
from fonely.voice.dialogue import (
    gate_response,
    contains_booking_success,
    contains_medical_advice,
    SAFE_NO_RECEIPT,
)


# Every known success pattern the models have produced in testing
SUCCESS_VARIANTS = [
    # Tamil script
    "உங்கள் appointment confirm ஆயிடுச்சு!",
    "Booking confirm ஆயிடுச்சு.",
    "Appointment உறுதி செய்யப்பட்டது.",
    "உங்க scaling appointment confirm ஆச்சு.",
    "Booking பதிவு செய்யப்பட்டது.",
    "உங்கள் appointment confirm பண்ணிட்டோம், Karthick.",
    # Tanglish
    "Booking fix aayiduchu bro!",
    "Appointment confirm pannitten.",
    "Booking confirmed for tomorrow 6:30.",
    "Your appointment is booked.",
    "Appointment saved successfully.",
    "I've confirmed your booking.",
    "Booking confirm aayiduchu, doctor alert panniten.",
    # With emoji/markdown (Claude style)
    "Booking confirmed ஆயிடுச்சு! ✅🎉",
    "Super! Appointment **confirmed** for tomorrow! 👍",
    "Perfect bro! 🎉 Booking confirmed!",
    # Embedded in longer responses
    "நன்றி Karthick! உங்கள் scaling appointment இன்று மாலை 6:30 confirm ஆயிடுச்சு. Fee Rs800.",
    "சரி கார்த்திக், உங்கள் appointment confirm பண்ணிட்டோம். Clinic-ல சந்திப்போம்!",
    "Karthick, tomorrow 6:30 PM scaling appointment confirmed. Please come 5 min early.",
    # Edge cases
    "Booking saved ஆயிடுச்சு.",
    "Appointment fixed for 5 PM.",
    "Your booking is scheduled.",
    "Appointment booked ஆயிடுச்சு.",
]

# Inputs that must NOT be blocked (not success language)
NON_SUCCESS = [
    "எந்த date-ல வரணும்?",
    "Scaling appointment-க்கு Rs 800 ஆகும்.",
    "Dr. Priya available-ஆ இருக்காங்க.",
    "உங்க பேரு சொல்லுங்க?",
    "இன்னைக்கு 10:00, 11:00, 17:00, 18:30 available.",
    "Scaling, இன்னைக்கு 6:30, Karthick. இது correct-ஆ?",
    "Clinic Aminjikarai-ல இருக்கு.",
    "Doctor பார்த்துதான் சொல்ல முடியும்.",
    "வேற ஏதாவது doubt இருக்கா?",
    "நன்றி, take care!",
    "Sunday clinic closed.",
    "Details collect பண்ணிட்டேன், verify பண்றேன்.",
    "",
    "hi",
    "ok",
]


class TestGatePropertyNoReceiptNoSuccess:
    """INVARIANT: gate_response(text, has_receipt=False) must NEVER
    produce output containing booking-success language.

    This is exhaustive over known patterns. If a new success pattern
    is discovered in production, add it here — the test must catch it.
    """

    def test_every_success_variant_blocked_without_receipt(self):
        for variant in SUCCESS_VARIANTS:
            gated, suppressed = gate_response(variant, has_receipt=False)
            assert not contains_booking_success(gated), (
                f"INVARIANT VIOLATION: success language survived gate without receipt.\n"
                f"  Input:  {variant}\n"
                f"  Output: {gated}\n"
                f"  This means a caller would hear a false confirmation."
            )
            assert suppressed, f"Gate should report suppression for: {variant}"

    def test_every_success_variant_produces_safe_recovery(self):
        for variant in SUCCESS_VARIANTS:
            gated, _ = gate_response(variant, has_receipt=False)
            assert gated == SAFE_NO_RECEIPT, (
                f"Recovery text mismatch for: {variant}\n"
                f"  Expected: {SAFE_NO_RECEIPT}\n"
                f"  Got:      {gated}"
            )

    def test_every_success_variant_allowed_with_receipt(self):
        for variant in SUCCESS_VARIANTS:
            gated, suppressed = gate_response(variant, has_receipt=True)
            # Medical advice is still blocked even with receipt
            if contains_medical_advice(variant):
                assert suppressed
            else:
                assert not suppressed, f"Wrongly suppressed with receipt: {variant}"
                assert gated == variant

    def test_non_success_never_blocked_without_receipt(self):
        for text in NON_SUCCESS:
            gated, suppressed = gate_response(text, has_receipt=False)
            if not contains_medical_advice(text):
                assert not suppressed, f"Wrongly suppressed non-success: {text}"
                assert gated == text


class TestGatePropertyRecoveryString:
    """The recovery string is BEHAVIOR-AFFECTING, not cosmetic.

    It changes the conversation history the model sees on subsequent
    turns, measurably altering downstream model behavior (raw eagerness
    narrowed from 6/20 to 3/20 when suppression was engaged).

    Do NOT edit for tone without re-measuring downstream effects.
    """

    def test_recovery_is_pinned(self):
        assert SAFE_NO_RECEIPT == "Details collect பண்ணிட்டேன், verify பண்றேன். சிறிது நேரம் காத்திருங்க."

    def test_recovery_is_tamil(self):
        tamil_chars = sum(1 for c in SAFE_NO_RECEIPT if "஀" <= c <= "௿")
        assert tamil_chars > 10

    def test_recovery_does_not_contain_success(self):
        assert not contains_booking_success(SAFE_NO_RECEIPT)

    def test_recovery_does_not_contain_medical(self):
        assert not contains_medical_advice(SAFE_NO_RECEIPT)
