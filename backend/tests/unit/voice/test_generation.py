"""Tests for generation clock and token currency."""

from fonely.voice.generation import GenerationClock


def test_initial_state():
    clock = GenerationClock("sess-1")
    token = clock.current()
    assert token.turn_id == 0
    assert token.generation_id == 0
    assert token.session_id == "sess-1"


def test_next_turn_increments_both():
    clock = GenerationClock("sess-1")
    t1 = clock.next_turn()
    assert t1.turn_id == 1
    assert t1.generation_id == 1
    t2 = clock.next_turn()
    assert t2.turn_id == 2
    assert t2.generation_id == 2


def test_advance_generation_only():
    clock = GenerationClock("sess-1")
    clock.next_turn()
    t = clock.advance_generation()
    assert t.turn_id == 1
    assert t.generation_id == 2


def test_is_current():
    clock = GenerationClock("sess-1")
    t1 = clock.next_turn()
    assert clock.is_current(t1)
    t2 = clock.advance_generation()
    assert not clock.is_current(t1)
    assert clock.is_current(t2)


def test_stale_after_next_turn():
    clock = GenerationClock("sess-1")
    t1 = clock.next_turn()
    clock.next_turn()
    assert not clock.is_current(t1)


def test_wrong_session_not_current():
    clock = GenerationClock("sess-1")
    t = clock.next_turn()
    other = GenerationClock("sess-2")
    other_t = other.next_turn()
    assert not clock.is_current(other_t)
    assert not other.is_current(t)


def test_turn_count():
    clock = GenerationClock("sess-1")
    assert clock.turn_count == 0
    clock.next_turn()
    clock.next_turn()
    assert clock.turn_count == 2
