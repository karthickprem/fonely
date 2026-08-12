"""100-case deterministic conversation corpus.

Tests the BookingCollection state machine + dialogue gates through
complete multi-turn conversations. No LLM calls — deterministic only.
Covers Tamil script, Tanglish, Indian English, all booking fields,
medical safety, corrections, ambiguity, and adversarial cases.

Each case: caller turns → expected state transitions → expected gate behavior.
"""
from __future__ import annotations

import pytest
from datetime import date, time as dt_time

from fonely.voice.dialogue import (
    BookingCollection,
    gate_response,
    contains_medical_advice,
    contains_booking_success,
    extract_booking_time,
    get_terminal_response,
    detect_filler,
    count_questions,
    SAFE_NO_RECEIPT,
    _assistant_asks_name,
)
from fonely.voice.context import (
    AvailableSlot,
    DayAvailability,
    TrustedClock,
    resolve_relative_date,
)


# Shared test fixtures
CLOCK = TrustedClock(
    now_utc=None,
    business_timezone="Asia/Kolkata",
    business_date=date(2026, 8, 11),
    day_of_week="monday",
)
AVAIL = DayAvailability(
    business_date=date(2026, 8, 11),
    day_of_week="monday",
    is_operating_day=True,
    is_exception_day=False,
    available_slots=(
        AvailableSlot(1, "Dr. Priya", dt_time(10, 0), dt_time(10, 30), "scaling"),
        AvailableSlot(1, "Dr. Priya", dt_time(11, 0), dt_time(11, 30), "scaling"),
        AvailableSlot(1, "Dr. Priya", dt_time(17, 0), dt_time(17, 30), "scaling"),
        AvailableSlot(1, "Dr. Priya", dt_time(18, 30), dt_time(19, 0), "scaling"),
    ),
)


def step(bc, text, prev_assistant=""):
    resolved = resolve_relative_date(text, CLOCK)
    bc.update(text, resolved_date=resolved, availability=AVAIL, previous_assistant_text=prev_assistant)


# ============================================================
# SECTION A: BOOKING ACTIVATION (10 cases)
# ============================================================
class TestBookingActivation:
    def test_a01_tamil_appointment_venum(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        assert bc.active

    def test_a02_tanglish_fix_pannanum(self):
        bc = BookingCollection()
        step(bc, "scaling appointment fix pannanum")
        assert bc.active

    def test_a03_english_need_appointment(self):
        bc = BookingCollection()
        step(bc, "I need a dental appointment")
        assert bc.active

    def test_a04_tamil_script_booking(self):
        bc = BookingCollection()
        step(bc, "அப்பாயிண்ட்மெண்ட் புக் பண்ணனும்")
        assert bc.active

    def test_a05_doctor_paakkanum(self):
        bc = BookingCollection()
        step(bc, "doctor பாக்கணும்")
        assert bc.active

    def test_a06_scaling_venum(self):
        bc = BookingCollection()
        step(bc, "scaling வேணும்")
        assert bc.active

    def test_a07_not_booking_location(self):
        bc = BookingCollection()
        step(bc, "clinic எங்க இருக்கு?")
        assert not bc.active

    def test_a08_not_booking_fee(self):
        bc = BookingCollection()
        step(bc, "fee எவ்வளவு?")
        assert not bc.active

    def test_a09_not_booking_greeting(self):
        bc = BookingCollection()
        step(bc, "வணக்கம்")
        assert not bc.active

    def test_a10_romanized_venum(self):
        bc = BookingCollection()
        step(bc, "appointment venum")
        assert bc.active


# ============================================================
# SECTION B: DATE RESOLUTION (15 cases)
# ============================================================
class TestDateResolution:
    def test_b01_innaikku(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு")
        assert bc.target_date == date(2026, 8, 11)

    def test_b02_naalaikku(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "நாளைக்கு")
        assert bc.target_date == date(2026, 8, 12)

    def test_b03_today_english(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "today")
        assert bc.target_date == date(2026, 8, 11)

    def test_b04_tomorrow_english(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "tomorrow")
        assert bc.target_date == date(2026, 8, 12)

    def test_b05_innaikku_romanized(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "innaikku")
        assert bc.target_date == date(2026, 8, 11)

    def test_b06_naalaikku_romanized(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "naalaikku")
        assert bc.target_date == date(2026, 8, 12)

    def test_b07_date_correction_today_to_tomorrow(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு")
        assert bc.target_date == date(2026, 8, 11)
        step(bc, "sorry நாளைக்கு change")
        assert bc.target_date == date(2026, 8, 12)
        assert bc.selected_time is None  # time reset on date change

    def test_b08_date_with_time_inline(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு 5 pm")
        assert bc.target_date == date(2026, 8, 11)
        assert bc.selected_time == dt_time(17, 0)

    def test_b09_date_embedded_in_sentence(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "நாளைக்கு scaling appointment வேணும்")
        assert bc.target_date == date(2026, 8, 12)

    def test_b10_no_date_random_text(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "scaling")
        assert bc.target_date is None

    def test_b11_innru_tamil_script(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்று")
        assert bc.target_date == date(2026, 8, 11)

    def test_b12_naalai_tamil_script(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "நாளை")
        assert bc.target_date == date(2026, 8, 12)

    def test_b13_date_correction_resets_time(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        assert bc.selected_time == dt_time(17, 0)
        step(bc, "நாளைக்கு change")
        assert bc.selected_time is None

    def test_b14_innaikkee(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கே")
        assert bc.target_date == date(2026, 8, 11)

    def test_b15_naalaikku_in_long_sentence(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "ok naalaikku porom")
        assert bc.target_date == date(2026, 8, 12)


# ============================================================
# SECTION C: TIME EXTRACTION + MATCHING (15 cases)
# ============================================================
class TestTimeExtraction:
    def test_c01_5pm(self):
        assert extract_booking_time("5 pm") == dt_time(17, 0)

    def test_c02_10am(self):
        assert extract_booking_time("10 am") == dt_time(10, 0)

    def test_c03_630(self):
        assert extract_booking_time("6:30") == dt_time(6, 30)

    def test_c04_1830(self):
        assert extract_booking_time("18:30") == dt_time(18, 30)

    def test_c05_tamil_5_mani(self):
        assert extract_booking_time("5 மணிக்கு") == dt_time(5, 0)

    def test_c06_tamil_ainthu(self):
        assert extract_booking_time("ஐந்து மணிக்கு") == dt_time(5, 0)

    def test_c07_tamil_pathu(self):
        assert extract_booking_time("பத்து மணிக்கு") == dt_time(10, 0)

    def test_c08_tamil_pannirandu(self):
        assert extract_booking_time("பன்னிரெண்டு மணிக்கு") == dt_time(12, 0)

    def test_c09_no_time_in_text(self):
        assert extract_booking_time("scaling appointment") is None

    def test_c10_time_with_offered_match(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        assert bc.selected_time == dt_time(17, 0)

    def test_c11_time_not_in_offered(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "3 pm")
        assert bc.selected_time is None

    def test_c12_ambiguous_5_without_meridiem(self):
        t = extract_booking_time("5 மணிக்கு")
        assert t == dt_time(5, 0)

    def test_c13_11_matches_offered(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "11 AM")
        assert bc.selected_time == dt_time(11, 0)

    def test_c14_630pm_matches_1830(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "6:30 pm")
        assert bc.selected_time == dt_time(18, 30)

    def test_c15_out_of_range_25(self):
        assert extract_booking_time("25:00") is None


# ============================================================
# SECTION D: NAME COLLECTION (10 cases)
# ============================================================
class TestNameCollection:
    def _setup_for_name(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        return bc

    def test_d01_full_name(self):
        bc = self._setup_for_name()
        step(bc, "Karthick", prev_assistant="உங்கள் பெயர் சொல்லுங்க")
        assert bc.patient_name == "Karthick"

    def test_d02_single_letter(self):
        bc = self._setup_for_name()
        step(bc, "B", prev_assistant="Patient name சொல்லுங்க")
        assert bc.patient_name == "B"

    def test_d03_tamil_name(self):
        bc = self._setup_for_name()
        step(bc, "முருகன்", prev_assistant="உங்க பேரு என்ன?")
        assert bc.patient_name == "முருகன்"

    def test_d04_name_not_set_without_prompt(self):
        bc = self._setup_for_name()
        step(bc, "Karthick", prev_assistant="எந்த நேரம் வசதி?")
        assert bc.patient_name is None

    def test_d05_date_word_not_name(self):
        bc = self._setup_for_name()
        step(bc, "tomorrow", prev_assistant="உங்கள் பெயர் என்ன?")
        assert bc.patient_name is None

    def test_d06_time_word_not_name(self):
        bc = self._setup_for_name()
        step(bc, "morning", prev_assistant="உங்கள் பெயர் சொல்லுங்க")
        assert bc.patient_name is None

    def test_d07_name_with_spaces(self):
        bc = self._setup_for_name()
        step(bc, "Karthick Prem", prev_assistant="Patient name please")
        assert bc.patient_name == "Karthick Prem"

    def test_d08_name_after_peyaril(self):
        bc = self._setup_for_name()
        step(bc, "Meena", prev_assistant="எந்த பெயரில் book பண்ணலாம்?")
        assert bc.patient_name == "Meena"

    def test_d09_medical_word_not_name(self):
        bc = self._setup_for_name()
        step(bc, "scaling", prev_assistant="உங்கள் பெயர் சொல்லுங்க")
        assert bc.patient_name is None

    def test_d10_nickname_accepted(self):
        bc = self._setup_for_name()
        step(bc, "Karthi", prev_assistant="Name சொல்லுங்க")
        assert bc.patient_name == "Karthi"


# ============================================================
# SECTION E: FIELD ORDER + REQUIRED FIELD (10 cases)
# ============================================================
class TestFieldOrder:
    def test_e01_initial_inactive(self):
        bc = BookingCollection()
        assert bc.required_field is None

    def test_e02_after_activation_needs_date(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        assert bc.required_field == "date"

    def test_e03_after_date_needs_time(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        step(bc, "இன்னைக்கு")
        assert bc.required_field == "time"

    def test_e04_after_time_needs_reason(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        assert bc.required_field == "reason"

    def test_e05_after_reason_needs_name(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        step(bc, "scaling வேணும்")
        assert bc.required_field == "name"

    def test_e06_after_name_needs_confirmation(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        step(bc, "Karthick", prev_assistant="உங்கள் பெயர் சொல்லுங்க")
        assert bc.required_field == "confirmation"

    def test_e07_reason_captured_inline(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        assert bc.reason is not None
        assert bc.required_field == "date"

    def test_e08_all_in_one_turn(self):
        bc = BookingCollection()
        step(bc, "நாளைக்கு scaling appointment 6:30 pm வேணும்")
        assert bc.active
        assert bc.target_date == date(2026, 8, 12)
        assert bc.selected_time == dt_time(18, 30)
        assert bc.reason is not None

    def test_e09_readback_format(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        step(bc, "Karthick", prev_assistant="உங்கள் பெயர் சொல்லுங்க")
        rb = bc.format_readback()
        assert rb is not None
        assert "Karthick" in rb
        assert "மாலை 5" in rb
        assert "correct" in rb.lower()

    def test_e10_render_shows_all_fields(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        rendered = bc.render()
        assert "active: true" in rendered
        assert "17:00" in rendered
        assert "2026-08-11" in rendered


# ============================================================
# SECTION F: MEDICAL SAFETY GATE (10 cases)
# ============================================================
class TestMedicalSafety:
    def test_f01_take_paracetamol(self):
        assert contains_medical_advice("take paracetamol")

    def test_f02_tamil_paracetamol_edukka(self):
        assert contains_medical_advice("paracetamol எடுக்கலாமா")

    def test_f03_crocin_podu(self):
        assert contains_medical_advice("crocin போடுங்க")

    def test_f04_dosage(self):
        assert contains_medical_advice("take 500mg twice daily")

    def test_f05_need_root_canal(self):
        assert contains_medical_advice("you need a root canal")

    def test_f06_could_be_infection(self):
        assert contains_medical_advice("could be an infection")

    def test_f07_safe_referral_not_advice(self):
        assert not contains_medical_advice("Doctor பார்த்துதான் சொல்ல முடியும்")

    def test_f08_booking_not_advice(self):
        assert not contains_medical_advice("Appointment book பண்ணலாமா?")

    def test_f09_gate_blocks_medical(self):
        gated, sup = gate_response("take paracetamol for pain", has_receipt=False)
        assert sup
        assert "doctor" in gated.lower() or "Doctor" in gated

    def test_f10_combiflam_tamil(self):
        assert contains_medical_advice("combiflam எடுத்துக்கோங்க")


# ============================================================
# SECTION G: RECEIPT-KEYED GATE (10 cases)
# ============================================================
class TestReceiptGate:
    def test_g01_success_blocked_no_receipt(self):
        gated, sup = gate_response("Booking confirmed!", has_receipt=False)
        assert sup
        assert gated == SAFE_NO_RECEIPT

    def test_g02_success_allowed_with_receipt(self):
        gated, sup = gate_response("Booking confirmed!", has_receipt=True)
        assert not sup
        assert gated == "Booking confirmed!"

    def test_g03_tamil_success_blocked(self):
        gated, sup = gate_response("Appointment confirm ஆயிடுச்சு!", has_receipt=False)
        assert sup

    def test_g04_non_success_passes(self):
        gated, sup = gate_response("எந்த date-ல வரணும்?", has_receipt=False)
        assert not sup
        assert gated == "எந்த date-ல வரணும்?"

    def test_g05_readback_question_passes(self):
        gated, sup = gate_response("Scaling, 2026-08-11 17:00, Karthick. இது correct-ஆ?", has_receipt=False)
        assert not sup

    def test_g06_embedded_success_blocked(self):
        gated, sup = gate_response("Karthick, appointment confirmed for tomorrow", has_receipt=False)
        assert sup

    def test_g07_saved_blocked(self):
        gated, sup = gate_response("Booking saved ஆயிடுச்சு", has_receipt=False)
        assert sup

    def test_g08_scheduled_blocked(self):
        gated, sup = gate_response("Your appointment is scheduled", has_receipt=False)
        assert sup

    def test_g09_recovery_text_stable(self):
        assert SAFE_NO_RECEIPT == "Details collect பண்ணிட்டேன், verify பண்றேன். சிறிது நேரம் காத்திருங்க."

    def test_g10_recovery_not_success(self):
        assert not contains_booking_success(SAFE_NO_RECEIPT)


# ============================================================
# SECTION H: FULL CONVERSATION FLOWS (10 cases)
# ============================================================
class TestFullConversations:
    def test_h01_happy_path_tamil(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        assert bc.active and bc.reason
        step(bc, "இன்னைக்கு")
        assert bc.target_date == date(2026, 8, 11)
        step(bc, "5 pm")
        assert bc.selected_time == dt_time(17, 0)
        step(bc, "கார்த்திக்", prev_assistant="உங்கள் பெயர் சொல்லுங்க")
        assert bc.patient_name == "கார்த்திக்"
        assert bc.required_field == "confirmation"
        rb = bc.format_readback()
        assert "கார்த்திக்" in rb and "மாலை 5" in rb

    def test_h02_tanglish_path(self):
        bc = BookingCollection()
        step(bc, "bro scaling appointment fix pannanum")
        assert bc.active
        step(bc, "tomorrow")
        assert bc.target_date == date(2026, 8, 12)
        step(bc, "6:30 pm")
        assert bc.selected_time == dt_time(18, 30)
        step(bc, "Karthick da", prev_assistant="Patient name சொல்லுங்க")
        assert bc.patient_name == "Karthick da"

    def test_h03_english_path(self):
        bc = BookingCollection()
        step(bc, "I need to book a dental appointment")
        assert bc.active
        step(bc, "consultation")
        step(bc, "today")
        assert bc.target_date == date(2026, 8, 11)
        step(bc, "11 AM")
        assert bc.selected_time == dt_time(11, 0)

    def test_h04_date_correction_mid_flow(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        assert bc.selected_time == dt_time(17, 0)
        step(bc, "நாளைக்கு change பண்ணுங்க")
        assert bc.target_date == date(2026, 8, 12)
        assert bc.selected_time is None

    def test_h05_reason_and_date_in_one(self):
        bc = BookingCollection()
        step(bc, "நாளைக்கு scaling appointment வேணும்")
        assert bc.active
        assert bc.reason is not None
        assert bc.target_date == date(2026, 8, 12)

    def test_h06_all_fields_one_turn(self):
        bc = BookingCollection()
        step(bc, "நாளைக்கு மாலை 6:30 scaling appointment வேணும்")
        assert bc.active
        assert bc.target_date == date(2026, 8, 12)
        assert bc.selected_time == dt_time(18, 30)
        assert bc.reason is not None

    def test_h07_time_not_available(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "3 pm")
        assert bc.selected_time is None

    def test_h08_non_booking_question(self):
        bc = BookingCollection()
        step(bc, "clinic எங்க இருக்கு?")
        assert not bc.active

    def test_h09_implicit_booking(self):
        bc = BookingCollection()
        step(bc, "doctor பாக்கணும்")
        assert bc.active

    def test_h10_short_name_after_inline_fields(self):
        bc = BookingCollection()
        step(bc, "scaling appointment நாளைக்கு 6:30 pm வேணும்")
        assert bc.active
        step(bc, "B", prev_assistant="உங்கள் பெயர் சொல்லுங்க")
        assert bc.patient_name == "B"


# ============================================================
# SECTION I: ADVERSARIAL / EDGE CASES (10 cases)
# ============================================================
class TestAdversarial:
    def test_i01_medical_then_booking(self):
        """Caller asks medical question, then wants to book."""
        bc = BookingCollection()
        step(bc, "பல்லு வலிக்குது, medicine வேணும்")
        assert not bc.active
        step(bc, "appointment book பண்ணணும்")
        assert bc.active

    def test_i02_tomorrow_when_name_asked(self):
        """'tomorrow' should not be accepted as name."""
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        step(bc, "tomorrow", prev_assistant="உங்கள் பெயர் சொல்லுங்க")
        assert bc.patient_name is None

    def test_i03_naalaikku_not_name(self):
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        step(bc, "naalaikku", prev_assistant="Name சொல்லுங்க")
        assert bc.patient_name is None

    def test_i04_service_word_not_name(self):
        bc = BookingCollection()
        step(bc, "appointment book பண்ணணும்")
        step(bc, "இன்னைக்கு")
        step(bc, "5 pm")
        step(bc, "scaling", prev_assistant="உங்கள் பெயர் சொல்லுங்க")
        assert bc.patient_name is None

    def test_i05_confirmation_without_all_fields(self):
        """'ஆமா' before all fields collected is not confirmation."""
        bc = BookingCollection()
        step(bc, "scaling appointment வேணும்")
        step(bc, "ஆமா")
        assert bc.required_field == "date"

    def test_i06_empty_text(self):
        bc = BookingCollection()
        step(bc, "")
        assert not bc.active

    def test_i07_gate_double_suppression(self):
        """Medical takes priority over receipt gate."""
        gated, sup = gate_response("take paracetamol, booking confirmed", has_receipt=False)
        assert sup
        assert "doctor" in gated.lower() or "Doctor" in gated

    def test_i08_filler_detection(self):
        assert detect_filler("Let me check the available slots")
        assert detect_filler("Sure, I can help you")
        assert not detect_filler("10:00, 11:00 slots available")

    def test_i09_question_count(self):
        assert count_questions("எந்த date வேணும்?") == 1
        assert count_questions("scaling-ஆ? date என்ன?") == 2
        assert count_questions("சரி booking பண்ணிட்டேன்.") == 0

    def test_i10_terminal_response(self):
        resp = get_terminal_response("abandoned", "ta-Latn")
        assert "booking" in resp.lower() or "call" in resp.lower()


# ============================================================
# SECTION J: ASSISTANT NAME DETECTION (10 cases)
# ============================================================
class TestAssistantNameDetection:
    def test_j01_english_name(self):
        assert _assistant_asks_name("Patient name please")

    def test_j02_tamil_peyar(self):
        assert _assistant_asks_name("உங்கள் பெயர் சொல்லுங்க")

    def test_j03_tamil_peru(self):
        assert _assistant_asks_name("உங்க பேரு என்ன?")

    def test_j04_tamil_peyaril(self):
        assert _assistant_asks_name("எந்த பெயரில் book பண்ணலாம்?")

    def test_j05_tanglish_name(self):
        assert _assistant_asks_name("Name சொல்லுங்க")

    def test_j06_nem(self):
        assert _assistant_asks_name("நேம் சொல்லுங்க")

    def test_j07_not_name_time_question(self):
        assert not _assistant_asks_name("என்ன நேரம் வசதி?")

    def test_j08_not_name_date_question(self):
        assert not _assistant_asks_name("எந்த date வேணும்?")

    def test_j09_not_name_greeting(self):
        assert not _assistant_asks_name("வணக்கம், எப்படி help பண்ணலாம்?")

    def test_j10_embedded_in_sentence(self):
        assert _assistant_asks_name("சரி, உங்கள் பெயர் என்ன?")
