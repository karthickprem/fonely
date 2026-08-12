"""Tests for the session-open sequence (DPDP notice before capture)."""
from __future__ import annotations

import pytest

from fonely.voice.session_open import open_session, SessionOpening
from fonely.domain.compliance.consent import NOTICE_VERSION


CLINIC = "Smile Care Dental Clinic"


class TestOpenSession:
    def test_notice_is_first_spoken_line(self):
        opening = open_session(clinic_name=CLINIC, greeting_text="வணக்கம்", locale="ta-IN")
        # The DPDP notice MUST precede the greeting.
        assert opening.spoken_lines[0] == opening.notice_text
        assert opening.spoken_lines[1] == "வணக்கம்"

    def test_notice_names_the_clinic_not_fonely(self):
        opening = open_session(clinic_name=CLINIC, greeting_text="hi", locale="ta-IN")
        assert CLINIC in opening.notice_text
        assert "Fonely" not in opening.notice_text  # clinic is the fiduciary

    def test_tamil_notice_says_voice_not_recorded(self):
        opening = open_session(clinic_name=CLINIC, greeting_text="hi", locale="ta-IN")
        # The load-bearing claim, in Tamil script.
        assert "குரல் பதிவு செய்யப்படாது" in opening.notice_text

    def test_english_locale(self):
        opening = open_session(clinic_name=CLINIC, greeting_text="hi", locale="en-IN")
        assert "automated booking assistant" in opening.notice_text
        assert "voice is not recorded" in opening.notice_text

    def test_evidence_event_carries_version_and_text(self):
        opening = open_session(clinic_name=CLINIC, greeting_text="hi", locale="ta-IN")
        ev = opening.notice_event
        assert ev["kind"] == "dpdp_notice"
        assert ev["notice_version"] == NOTICE_VERSION
        assert ev["locale"] == "ta-IN"
        # The spoken text is persisted, not just the version number.
        assert ev["text"] == opening.notice_text

    def test_version_recorded(self):
        opening = open_session(clinic_name=CLINIC, greeting_text="hi")
        assert opening.notice_version == NOTICE_VERSION

    def test_missing_clinic_name_raises(self):
        # A notice that cannot name the fiduciary is not a notice — fail, do
        # not speak around the gap.
        with pytest.raises(ValueError):
            open_session(clinic_name="", greeting_text="hi", locale="ta-IN")

    def test_unknown_locale_degrades_to_english_not_silence(self):
        # An unknown locale gives the English notice, never silence — a patient
        # in the wrong language was still given a notice.
        opening = open_session(clinic_name=CLINIC, greeting_text="hi", locale="xx-YY")
        assert "automated booking assistant" in opening.notice_text
