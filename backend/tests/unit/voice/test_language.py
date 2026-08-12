"""Tests for the language-mirroring layer.

Covers sticky script-based detection (including the mid-conversation flip), the
completeness of the 3-bucket response table, and the load-bearing invariant:
a readback's FACTS (date, time value, name) are byte-identical across all three
languages — only the connective words differ. A readback whose time drifted
between languages would be a wrong-booking bug, so this is asserted directly.
"""

from __future__ import annotations

from datetime import date, time

from fonely.voice.dialogue import BookingCollection
from fonely.voice.language import (
    DEFAULT_LANGUAGE,
    RESPONSES,
    detect_language,
    format_time_spoken,
    get_response,
)


class TestDetection:
    def test_pure_english(self):
        assert detect_language("I need a dental appointment tomorrow", "ta-Latn") == "en"

    def test_tanglish_romanized(self):
        assert detect_language("naalaikku scaling venum", "en") == "ta-Latn"

    def test_tamil_script(self):
        assert detect_language("நாளைக்கு scaling வேணும்", "en") == "ta"

    def test_tamil_script_wins_over_latin(self):
        # Mixed but has Tamil script → ta
        assert detect_language("scaling வேணும் tomorrow", "en") == "ta"

    def test_chennai_particle_is_tanglish(self):
        assert detect_language("Bro scaling appointment fix pannanum da", "en") == "ta-Latn"


class TestStickiness:
    def test_bare_number_keeps_previous(self):
        assert detect_language("6:30", "en") == "en"
        assert detect_language("6:30", "ta") == "ta"

    def test_single_confirm_word_keeps_previous(self):
        assert detect_language("ok", "ta-Latn") == "ta-Latn"
        assert detect_language("yes", "ta") == "ta"

    def test_single_letter_name_keeps_previous(self):
        assert detect_language("B", "en") == "en"
        assert detect_language("K", "ta") == "ta"

    def test_empty_keeps_previous(self):
        assert detect_language("", "ta-Latn") == "ta-Latn"

    def test_invalid_previous_falls_to_default(self):
        assert detect_language("6:30", "xx") == DEFAULT_LANGUAGE


class TestFlip:
    def test_english_to_tamil_midconversation(self):
        lang = DEFAULT_LANGUAGE
        lang = detect_language("I want an appointment", lang)
        assert lang == "en"
        lang = detect_language("tomorrow", lang)  # 'tomorrow' is a real English word
        assert lang == "en"
        lang = detect_language("actually தமிழ்ல sollunga", lang)  # flips
        assert lang == "ta"

    def test_tanglish_to_english(self):
        lang = "ta-Latn"
        lang = detect_language("scaling venum naalaikku", lang)
        assert lang == "ta-Latn"
        lang = detect_language("actually please make it evening instead", lang)
        assert lang == "en"


class TestResponseTable:
    def test_every_key_has_all_three_buckets(self):
        """A missing variant would silently fall back to English at a possibly
        safety-critical moment. Every key must carry all three."""
        for key, variants in RESPONSES.items():
            assert set(variants.keys()) == {"en", "ta", "ta-Latn"}, (
                f"key '{key}' missing a language bucket: {sorted(variants)}"
            )

    def test_get_response_returns_requested_language(self):
        assert get_response("goodbye", "en") == RESPONSES["goodbye"]["en"]
        assert get_response("goodbye", "ta") == RESPONSES["goodbye"]["ta"]

    def test_get_response_falls_back_to_english(self):
        assert get_response("goodbye", "unknown") == RESPONSES["goodbye"]["en"]

    def test_commit_success_carries_id_placeholder_in_all_langs(self):
        for lang in ("en", "ta", "ta-Latn"):
            assert "{id}" in RESPONSES["commit_success"][lang]

    def test_medical_safe_present_in_all_langs(self):
        # Safety string must be equally locked in every bucket.
        for lang in ("en", "ta", "ta-Latn"):
            assert get_response("medical_safe", lang).strip() != ""


class TestSpokenTime:
    def test_english_period_word(self):
        assert "evening" in format_time_spoken(time(18, 30), "en")
        assert "morning" in format_time_spoken(time(10, 0), "en")
        assert "afternoon" in format_time_spoken(time(14, 0), "en")

    def test_tamil_period_word(self):
        assert "மாலை" in format_time_spoken(time(18, 30), "ta")
        assert "காலை" in format_time_spoken(time(10, 0), "ta")

    def test_time_value_identical_across_languages(self):
        """The digit part of the time is the same in all three languages —
        only the period word changes. This is the wrong-booking guard."""
        t = time(18, 30)
        for lang in ("en", "ta", "ta-Latn"):
            assert "6:30" in format_time_spoken(t, lang)


class TestReadbackFactsIdentical:
    """THE load-bearing test: a readback in three languages must embed the
    SAME facts. Service name, date, time value, and patient name are identical;
    only the period word and the trailing question differ. A drift here is a
    wrong-day / wrong-time booking with a perfect-looking transcript."""

    def _complete_booking(self):
        bc = BookingCollection()
        bc.active = True
        bc.reason = "scaling"
        bc.target_date = date(2026, 8, 12)
        bc.selected_time = time(18, 30)
        bc.patient_name = "Karthick"
        assert bc.required_field == "confirmation"
        return bc

    def test_facts_present_in_all_three(self):
        bc = self._complete_booking()
        for lang in ("en", "ta", "ta-Latn"):
            rb = bc.format_readback(lang)
            assert rb is not None
            # Same service, same date, same time digits, same name in every language.
            assert "Scaling" in rb
            assert "August 12" in rb
            assert "6:30" in rb
            assert "Karthick" in rb

    def test_only_connectives_differ(self):
        bc = self._complete_booking()
        en = bc.format_readback("en")
        ta = bc.format_readback("ta")
        tanglish = bc.format_readback("ta-Latn")
        # English uses "evening" + "Is this correct?"
        assert "evening" in en and "Is this correct?" in en
        # Tamil uses மாலை + இது சரியா?
        assert "மாலை" in ta and "சரியா" in ta
        # Tanglish uses மாலை + correct-ஆ
        assert "மாலை" in tanglish and "correct-ஆ" in tanglish

    def test_time_digits_match_commit_value(self):
        """The time shown in every readback language is the exact selected_time,
        so it matches what the commit path uses. No language re-derives it."""
        bc = self._complete_booking()
        for lang in ("en", "ta", "ta-Latn"):
            rb = bc.format_readback(lang)
            # selected_time is 18:30 → spoken 6:30; commit uses 18:30 directly.
            assert "6:30" in rb
            assert bc.selected_time == time(18, 30)  # unchanged by formatting
