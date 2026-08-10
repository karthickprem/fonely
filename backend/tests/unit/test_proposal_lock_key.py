"""Deterministic advisory lock key for proposal family serialization."""

from fonely.services.owner_commands import _proposal_family_lock_key

_D1 = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
_D2 = "ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00"


def test_lock_key_is_deterministic() -> None:
    assert _proposal_family_lock_key(1, _D1) == _proposal_family_lock_key(1, _D1)


def test_lock_key_differs_by_business() -> None:
    assert _proposal_family_lock_key(1, _D1) != _proposal_family_lock_key(2, _D1)


def test_lock_key_differs_by_semantic_digest() -> None:
    assert _proposal_family_lock_key(1, _D1) != _proposal_family_lock_key(1, _D2)


def test_lock_key_is_signed_int64() -> None:
    key = _proposal_family_lock_key(1, _D1)
    assert isinstance(key, int)
    assert -(2**63) <= key <= 2**63 - 1


def test_lock_key_stable_across_calls() -> None:
    keys = [_proposal_family_lock_key(42, _D1) for _ in range(100)]
    assert len(set(keys)) == 1


def test_lock_key_golden_vector() -> None:
    assert _proposal_family_lock_key(1, _D1) == -5008738171112013690
    assert _proposal_family_lock_key(2, _D1) == -2145449866162205489
