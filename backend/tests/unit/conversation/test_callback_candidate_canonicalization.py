"""Canonicalization of give-up callback candidates (#41 release-blocker fix).

The voice give-up callback (#36/#41) carries the ambiguous doctor names into a
``CallbackData`` payload whose ``attempted_candidates`` constrains each element
``min_length=1, max_length=200`` and the list ``max_length=20``. The ORIGINAL
construction was ``[str(c.get("name", "")) for c in cand]``, which turns a
missing/blank name into ``""`` (a schema violation that raises inside
PendingAction.create) and ``None`` into the literal string ``"None"``. Because
``_persist_voice_callback`` persists best-effort, that ValidationError is
swallowed and the DURABLE callback is silently dropped — the exact
"absence reads as success" trap the callback exists to prevent.

``_canonical_callback_candidates`` closes that: it extracts ``name`` from each
candidate mapping and keeps only non-blank strings, stripped, truncated to 200
chars, de-duplicated in first-seen order, capped at 20. All-invalid input yields
``[]`` (schema-legal — the list has no ``min_length``) so the callback still
persists, rather than inventing a misleading placeholder name.

These are pure-function tests (no DB); the real give-up path is proven end to
end against PostgreSQL in test_voice_callback_postgres.py.
"""

import pytest
from pydantic import ValidationError

# Import-origin guard: exercise THIS checkout's src, not an installed copy.
import fonely.services.conversation as _conv_mod
from fonely.domain.pending_actions.payloads import CallbackData
from fonely.services.conversation import _canonical_callback_candidates

# The exact per-element bounds the payload schema enforces, mirrored here so a
# schema change that loosens/tightens them makes this test fail loudly rather
# than silently drift.
_MAX_NAME_LEN = 200
_MAX_COUNT = 20


def _old_construction(cand: list) -> list[str]:
    """The pre-fix comprehension, kept verbatim as the mutation baseline."""
    return [str(c.get("name", "")) for c in cand]


def test_module_under_test_is_this_checkout() -> None:
    assert "/backend/src/fonely/services/conversation.py" in _conv_mod.__file__


def test_missing_name_key_is_dropped() -> None:
    assert _canonical_callback_candidates([{"id": 1}]) == []


def test_none_name_is_dropped_not_stringified() -> None:
    # The old code produced "None" (a real, misleading display string). The fix
    # drops it entirely.
    assert _canonical_callback_candidates([{"id": 1, "name": None}]) == []


def test_whitespace_only_name_is_dropped() -> None:
    assert _canonical_callback_candidates([{"id": 1, "name": "   \t \n"}]) == []


def test_empty_string_name_is_dropped() -> None:
    assert _canonical_callback_candidates([{"id": 1, "name": ""}]) == []


def test_non_mapping_candidate_is_dropped() -> None:
    assert _canonical_callback_candidates(["Dr. Priya", 42, None, ["x"]]) == []


def test_non_string_name_is_dropped() -> None:
    assert _canonical_callback_candidates([{"id": 1, "name": 123}]) == []


def test_valid_names_preserved_in_first_seen_order() -> None:
    got = _canonical_callback_candidates(
        [{"id": 2, "name": "Dr. Priya"}, {"id": 1, "name": "Dr. Arun"}]
    )
    assert got == ["Dr. Priya", "Dr. Arun"]


def test_names_are_stripped() -> None:
    assert _canonical_callback_candidates([{"name": "  Dr. Priya  "}]) == ["Dr. Priya"]


def test_duplicate_names_collapse_on_canonical_form() -> None:
    # Two doctors sharing a display name (or the same one repeated after
    # stripping) appear once — the human needs the distinct choices, not noise.
    got = _canonical_callback_candidates(
        [{"id": 1, "name": "Dr. Priya"}, {"id": 2, "name": "  Dr. Priya "}]
    )
    assert got == ["Dr. Priya"]


def test_overlength_name_truncated_to_schema_bound() -> None:
    got = _canonical_callback_candidates([{"name": "X" * 500}])
    assert len(got) == 1
    assert len(got[0]) == _MAX_NAME_LEN


def test_count_capped_at_schema_bound_keeping_first_seen() -> None:
    raw = [{"id": i, "name": f"Dr. {i:03d}"} for i in range(50)]
    got = _canonical_callback_candidates(raw)
    assert len(got) == _MAX_COUNT
    assert got[0] == "Dr. 000"
    assert got[-1] == "Dr. 019"


def test_all_invalid_yields_empty_list_that_schema_accepts() -> None:
    raw = [{"id": 1}, {"name": None}, {"name": "  "}, "nope", 7]
    got = _canonical_callback_candidates(raw)
    assert got == []
    # The empty list is schema-legal — this is what lets the callback persist
    # instead of being dropped. Proven by constructing the real payload model.
    model = CallbackData(
        reason_code="doctor_disambiguation_exhausted",
        caller_phone="+919123456789",
        attempted_candidates=got,
        requested_at="2026-08-15T10:00:00+00:00",
    )
    assert model.attempted_candidates == []


def test_mixed_valid_and_invalid_keeps_only_valid_in_order() -> None:
    raw = [
        {"id": 1, "name": "  Dr. Priya  "},
        {"name": None},
        {"name": ""},
        {"id": 2, "name": "Dr. Arun"},
        {"id": 3},
        {"id": 4, "name": "Dr. Priya"},  # dup of the first, canonical
    ]
    assert _canonical_callback_candidates(raw) == ["Dr. Priya", "Dr. Arun"]


# --- Mutation proof: the OLD construction produces schema-violating output that
# --- the fix eliminates. This is the regression guard.


def test_old_construction_produces_schema_violation_that_new_avoids() -> None:
    """A candidate with a missing name makes the OLD comprehension emit ``""``,
    which violates CallbackData min_length=1 and raises — the failure that the
    best-effort catch swallowed, silently dropping the callback. The NEW helper
    yields a schema-valid list on the same input."""
    cand = [{"id": 1, "name": "Dr. Priya"}, {"id": 2}]  # second has no name

    old = _old_construction(cand)
    assert "" in old, "baseline: old code emits an empty-string candidate"
    with pytest.raises(ValidationError):
        CallbackData(
            reason_code="doctor_disambiguation_exhausted",
            caller_phone="+919123456789",
            attempted_candidates=old,
            requested_at="2026-08-15T10:00:00+00:00",
        )

    new = _canonical_callback_candidates(cand)
    assert new == ["Dr. Priya"]
    # The new list builds a valid payload — no exception, callback survives.
    model = CallbackData(
        reason_code="doctor_disambiguation_exhausted",
        caller_phone="+919123456789",
        attempted_candidates=new,
        requested_at="2026-08-15T10:00:00+00:00",
    )
    assert model.attempted_candidates == ["Dr. Priya"]


def test_old_construction_stringifies_none_where_new_drops_it() -> None:
    """The old code turned a ``None`` name into the literal ``"None"`` — a
    schema-valid but MISLEADING display string a human would read as a doctor
    called "None". The fix drops it."""
    cand = [{"id": 1, "name": None}]
    assert _old_construction(cand) == ["None"]
    assert _canonical_callback_candidates(cand) == []
