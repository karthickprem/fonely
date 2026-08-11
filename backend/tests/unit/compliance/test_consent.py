"""Tests for the DPDP notice a patient hears before they speak.

These assert the notice's *obligations*, not its exact wording -- the Tamil
still has to survive native review and the English will get tightened, and a
test pinned to a literal string would have to be rewritten every time without
ever having checked anything. What must not change is that the clinic is
named, the purpose is stated, the recording claim is made, and Fonely is not
mentioned to the patient.
"""

from __future__ import annotations

import pytest

from fonely.domain.compliance.consent import (
    NOTICE_VERSION,
    build_grievance_notice,
    build_opening_notice,
    notice_transcript_event,
)

_CLINIC = "Smile Dental"
_LOCALES = ["ta-IN", "en-IN"]


class TestOpeningNotice:
    @pytest.mark.parametrize("locale", _LOCALES)
    def test_names_the_clinic(self, locale: str) -> None:
        """The patient must be told whose assistant this is."""
        assert _CLINIC in build_opening_notice(_CLINIC, locale)

    @pytest.mark.parametrize("locale", _LOCALES)
    def test_never_names_fonely(self, locale: str) -> None:
        """The clinic is the fiduciary; we are the processor.

        Naming Fonely to a patient points them at a company that has no
        relationship with them and no authority to answer them.
        """
        assert "fonely" not in build_opening_notice(_CLINIC, locale).lower()

    @pytest.mark.parametrize("locale", _LOCALES)
    def test_is_short_enough_to_be_heard(self, locale: str) -> None:
        """A notice nobody listens to the end of is not a notice.

        This plays before the patient has said a word. The bound is generous
        -- it is there to fail loudly if someone pastes a paragraph of policy
        in, not to police a word or two.
        """
        assert len(build_opening_notice(_CLINIC, locale)) < 200

    def test_english_states_purpose_and_recording(self) -> None:
        """Asserted on English only: the Tamil equivalent is checked by a
        human reviewer, and a keyword assertion on Tamil would just be a
        second copy of the string."""
        notice = build_opening_notice(_CLINIC, "en-IN").lower()
        assert "booking" in notice
        assert "not recorded" in notice
        assert "automated" in notice

    def test_unknown_locale_falls_back_rather_than_failing(self) -> None:
        """Wrong language beats silence -- the patient is still told."""
        notice = build_opening_notice(_CLINIC, "kn-IN")
        assert notice == build_opening_notice(_CLINIC, "en-IN")

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_missing_clinic_name_raises(self, blank: str) -> None:
        """Better a failure at call setup than "this is 's assistant"."""
        with pytest.raises(ValueError, match="clinic_name"):
            build_opening_notice(blank, "ta-IN")

    def test_clinic_name_is_trimmed(self) -> None:
        assert build_opening_notice("  Smile Dental  ") == build_opening_notice("Smile Dental")


class TestGrievanceNotice:
    @pytest.mark.parametrize("locale", _LOCALES)
    def test_carries_clinic_and_contact(self, locale: str) -> None:
        notice = build_grievance_notice(_CLINIC, "+919000000000", locale)
        assert _CLINIC in notice
        assert "+919000000000" in notice

    def test_contact_is_not_in_the_opening(self) -> None:
        """Deliberate: the opening's attention budget buys disclosure, not a
        phone number nobody writes down."""
        assert "+919000000000" not in build_opening_notice(_CLINIC, "ta-IN")

    def test_missing_contact_raises(self) -> None:
        with pytest.raises(ValueError, match="contact"):
            build_grievance_notice(_CLINIC, "  ")

    def test_missing_clinic_raises(self) -> None:
        with pytest.raises(ValueError, match="clinic_name"):
            build_grievance_notice("", "+919000000000")


class TestTranscriptEvent:
    def test_records_what_was_actually_said(self) -> None:
        """The version alone only identifies the text if this file's history
        survives. Store the words."""
        spoken = build_opening_notice(_CLINIC, "ta-IN")
        event = notice_transcript_event("ta-IN", spoken)
        assert event["text"] == spoken
        assert event["locale"] == "ta-IN"
        assert event["notice_version"] == NOTICE_VERSION
        assert event["kind"] == "dpdp_notice"

    def test_event_is_json_serialisable(self) -> None:
        """It is written into a JSONB column, so this is not academic."""
        import json

        json.dumps(notice_transcript_event("ta-IN", build_opening_notice(_CLINIC, "ta-IN")))
