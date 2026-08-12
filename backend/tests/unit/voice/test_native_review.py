"""Tests for naturalness heuristics and review worksheet."""
from fonely.voice.native_review import (
    ReviewWorksheet,
    check_naturalness,
)


class TestNaturalnessChecks:
    def test_clean_tanglish_passes(self):
        checks = check_naturalness("நாளைக்கு 10, 6:30 available. Consultation ₹300.")
        assert all(c.passed for c in checks)

    def test_formal_tamil_detected(self):
        checks = check_naturalness("நான் செய்கிறேன் appointment booking")
        formal = next(c for c in checks if c.name == "no_formal_tamil")
        assert not formal.passed

    def test_isolated_suffix_detected(self):
        checks = check_naturalness("Available ஆ ")
        suffix = next(c for c in checks if c.name == "no_isolated_suffix")
        assert not suffix.passed

    def test_foreign_script_detected(self):
        checks = check_naturalness("అపాయింట్‌మెంట్ booking")
        foreign = next(c for c in checks if c.name == "no_telugu_kannada")
        assert not foreign.passed

    def test_emoji_detected(self):
        checks = check_naturalness("Appointment confirmed! 😊")
        emoji = next(c for c in checks if c.name == "no_emoji")
        assert not emoji.passed

    def test_markdown_detected(self):
        checks = check_naturalness("**Booking** details: *scaling*")
        md = next(c for c in checks if c.name == "no_markdown")
        assert not md.passed


class TestReviewWorksheet:
    def test_add_and_summary(self):
        ws = ReviewWorksheet(reviewer_name="Test")
        ws.add_entry("AC-001", 1, "Aminjikarai-ல இருக்கு. Consultation ₹300.")
        ws.add_entry("AC-001", 2, "நாளைக்கு 10, 6:30 available.")
        summary = ws.summary()
        assert summary["total_entries"] == 2
        assert summary["pending_native_review"] == 2

    def test_native_rating(self):
        ws = ReviewWorksheet()
        entry = ws.add_entry("AC-002", 1, "Dr. Priya 10:00 available.")
        entry.native_rating = 4
        entry.native_notes = "Natural Tanglish"
        summary = ws.summary()
        assert summary["native_rated"] == 1
