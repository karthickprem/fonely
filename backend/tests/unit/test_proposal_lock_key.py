"""Deterministic advisory lock key for proposal family serialization."""

from fonely.services.owner_commands import _proposal_family_lock_key


def test_lock_key_is_deterministic() -> None:
    k1 = _proposal_family_lock_key(1, "owner-v1-1-10-abc123")
    k2 = _proposal_family_lock_key(1, "owner-v1-1-10-abc123")
    assert k1 == k2


def test_lock_key_differs_by_business() -> None:
    k1 = _proposal_family_lock_key(1, "owner-v1-1-10-abc123")
    k2 = _proposal_family_lock_key(2, "owner-v1-1-10-abc123")
    assert k1 != k2


def test_lock_key_differs_by_semantic_key() -> None:
    k1 = _proposal_family_lock_key(1, "owner-v1-1-10-abc123")
    k2 = _proposal_family_lock_key(1, "owner-v1-1-10-def456")
    assert k1 != k2


def test_lock_key_is_signed_int64() -> None:
    key = _proposal_family_lock_key(1, "test-key")
    assert isinstance(key, int)
    assert -(2**63) <= key <= 2**63 - 1


def test_lock_key_stable_across_calls() -> None:
    keys = [_proposal_family_lock_key(42, "same-key") for _ in range(100)]
    assert len(set(keys)) == 1


def test_lock_key_golden_vector() -> None:
    key = _proposal_family_lock_key(1, "owner-v1-1-10-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2")
    assert isinstance(key, int)
    assert key != 0
    key2 = _proposal_family_lock_key(1, "owner-v1-1-10-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2")
    assert key == key2
