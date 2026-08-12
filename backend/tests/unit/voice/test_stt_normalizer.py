"""Post-STT normalizer tests — negative control included.

Tests both directions: strings that MUST normalize and strings that
must NOT. A normalizer is a lossy transform; false normalization is
a deterministic defect that fires every time.
"""

from fonely.voice.stt_normalizer import get_table_provenance, normalize


class TestMustNormalize:
    """Strings that MUST be normalized — the normalizer's job."""

    def test_romanized_tomorrow(self):
        r = normalize("naalaikku appointment venum")
        assert r.normalized == "நாளைக்கு appointment venum"
        assert r.raw == "naalaikku appointment venum"
        assert len(r.changes) > 0

    def test_romanized_today(self):
        r = normalize("innaikku doctor venum")
        assert "இன்னைக்கு" in r.normalized

    def test_romanized_variant_naalaiku(self):
        r = normalize("naalaiku scaling")
        assert "நாளைக்கு" in r.normalized

    def test_romanized_number_ainthu(self):
        r = normalize("ainthu mani")
        assert r.normalized == "5 mani"

    def test_romanized_number_pathu(self):
        r = normalize("pathu mani")
        assert r.normalized == "10 mani"

    def test_spelling_apointment(self):
        r = normalize("apointment book pannanum")
        assert "appointment" in r.normalized

    def test_number_word_six_thirty(self):
        r = normalize("six thirty slot venum")
        assert "6:30" in r.normalized

    def test_filler_removal_um(self):
        r = normalize("um, naalaikku venum")
        assert r.normalized.startswith("நாளைக்கு")
        assert not r.normalized.startswith("um")

    def test_filler_removal_actually(self):
        r = normalize("actually scaling venum")
        assert r.normalized == "scaling venum"

    def test_multiple_normalizations(self):
        r = normalize("um, innaikku ainthu mani apointment")
        assert "இன்னைக்கு" in r.normalized
        assert "5" in r.normalized
        assert "appointment" in r.normalized
        assert len(r.changes) >= 3


class TestMustNotNormalize:
    """Strings that must NOT be changed — false normalization is a defect."""

    def test_tamil_script_unchanged(self):
        r = normalize("இன்னைக்கு appointment வேணும்")
        assert r.normalized == "இன்னைக்கு appointment வேணும்"
        assert len(r.changes) == 0

    def test_correct_spelling_unchanged(self):
        r = normalize("appointment scaling doctor")
        assert r.normalized == "appointment scaling doctor"

    def test_pure_english_unchanged(self):
        r = normalize("I want to book an appointment")
        assert r.normalized == "I want to book an appointment"

    def test_five_not_resolved_to_pm(self):
        """Constraint 2: never resolve ambiguity. 5 stays 5."""
        r = normalize("ainthu mani")
        assert r.normalized == "5 mani"
        assert "17" not in r.normalized
        assert "PM" not in r.normalized
        assert "pm" not in r.normalized


class TestFieldAwareName:
    """Constraint 3: skip date/time/number normalization for names."""

    def test_name_aindu_not_normalized(self):
        r = normalize("Aindu", required_field="name")
        assert r.normalized == "Aindu"
        assert len(r.changes) == 0

    def test_name_nalini_not_normalized(self):
        r = normalize("Nalini", required_field="name")
        assert r.normalized == "Nalini"

    def test_name_naalai_not_normalized(self):
        """A person named Naalai should not become நாளை."""
        r = normalize("Naalai", required_field="name")
        assert r.normalized == "Naalai"

    def test_name_pathu_not_normalized(self):
        r = normalize("Pathu", required_field="name")
        assert r.normalized == "Pathu"

    def test_date_still_normalizes_for_non_name_field(self):
        r = normalize("naalaikku", required_field="date")
        assert r.normalized == "நாளைக்கு"

    def test_spelling_still_normalizes_for_name_field(self):
        """Spelling corrections are safe for names — they don't collide."""
        r = normalize("apointment booking for Aindu", required_field="name")
        assert "appointment" in r.normalized
        assert "Aindu" in r.normalized  # number NOT normalized


class TestRawPreserved:
    """Constraint 1: raw transcript always preserved."""

    def test_raw_never_modified(self):
        r = normalize("um, naalaikku ainthu mani apointment")
        assert r.raw == "um, naalaikku ainthu mani apointment"
        assert r.raw != r.normalized

    def test_identical_when_no_changes(self):
        r = normalize("scaling appointment")
        assert r.raw == r.normalized


class TestProvenance:
    """Constraint 4: every entry has provenance."""

    def test_all_entries_have_provenance(self):
        report = get_table_provenance()
        for table_name, entries in report.items():
            for entry in entries:
                assert entry["provenance"] in ("guessed", "observed"), (
                    f"{table_name}:{entry['pattern']} has invalid provenance: {entry['provenance']}"
                )

    def test_current_entries_all_guessed(self):
        """Until Tier B runs, all entries should be 'guessed'."""
        report = get_table_provenance()
        for table_name, entries in report.items():
            for entry in entries:
                assert entry["provenance"] == "guessed", (
                    f"{table_name}:{entry['pattern']} marked 'observed' without Tier B evidence"
                )


class TestChangesTracked:
    """Every normalization is logged for audit."""

    def test_changes_list_populated(self):
        r = normalize("naalaikku ainthu")
        assert len(r.changes) == 2
        assert any("date" in c for c in r.changes)
        assert any("time" in c for c in r.changes)

    def test_no_changes_empty(self):
        r = normalize("scaling")
        assert len(r.changes) == 0
