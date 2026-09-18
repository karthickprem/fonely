"""Executable reference canonicalizer for proposal family identity.

Produces deterministic family_id and lock_key vectors from command preimages.
Run: python3 docs/contract/canonicalizer.py
"""

import hashlib
import json
import struct
import sys

NAMESPACE = b"fonely.owner_proposal_family.v1"

# Per-command discriminated schemas (absent fields OMITTED, not null)
DOCTOR_LEAVE_PREIMAGE = {
    "family_schema": "owner-command-family-v1",
    "business_id": 1,
    "owner_user_id": 10,
    "command_type": "doctor_leave",
    "target_date": "2026-08-12",
    "target_timezone": "Asia/Kolkata",
    "resource_id": 1,
    "reason": "Leave",
}

CLOSE_CLINIC_PREIMAGE = {
    "family_schema": "owner-command-family-v1",
    "business_id": 1,
    "owner_user_id": 10,
    "command_type": "close_clinic",
    "target_date": "2026-08-12",
    "target_timezone": "Asia/Kolkata",
    "reason": "Holiday",
}

CLOSE_EARLY_PREIMAGE = {
    "family_schema": "owner-command-family-v1",
    "business_id": 1,
    "owner_user_id": 10,
    "command_type": "close_early",
    "target_date": "2026-08-12",
    "target_timezone": "Asia/Kolkata",
    "close_time": "17:00",
    "reason": "Closing early",
}

DOCTOR_LEAVE_B2 = {**DOCTOR_LEAVE_PREIMAGE, "business_id": 2}


def canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def family_id(preimage: dict) -> str:
    return hashlib.sha256(canonical_json(preimage).encode("utf-8")).hexdigest()


def lock_key(fid: str) -> int:
    digest = hashlib.blake2b(
        bytes.fromhex(fid), key=NAMESPACE, digest_size=8
    ).digest()
    return int(struct.unpack(">q", digest)[0])


def print_vector(name: str, preimage: dict) -> None:
    canonical = canonical_json(preimage)
    fid = family_id(preimage)
    lk = lock_key(fid)
    print(f"\n{name}:")
    print(f"  canonical: {canonical}")
    print(f"  family_id: {fid}")
    print(f"  lock_key:  {lk}")


def main() -> None:
    print("=== Proposal Family Identity Vectors ===")
    print(f"namespace: {NAMESPACE!r}")
    print(f"family_id algorithm: SHA-256")
    print(f"lock_key algorithm: BLAKE2b(key=namespace, data=family_id_bytes, digest_size=8) -> signed int64")

    print_vector("doctor_leave (b1)", DOCTOR_LEAVE_PREIMAGE)
    print_vector("close_clinic (b1)", CLOSE_CLINIC_PREIMAGE)
    print_vector("close_early (b1)", CLOSE_EARLY_PREIMAGE)
    print_vector("doctor_leave (b2)", DOCTOR_LEAVE_B2)

    # Cross-checks
    f1 = family_id(DOCTOR_LEAVE_PREIMAGE)
    f2 = family_id(DOCTOR_LEAVE_B2)
    assert f1 != f2, "same command different business must differ"

    fc = family_id(CLOSE_CLINIC_PREIMAGE)
    fe = family_id(CLOSE_EARLY_PREIMAGE)
    assert f1 != fc != fe, "different commands must differ"

    # Determinism
    for _ in range(1000):
        assert family_id(DOCTOR_LEAVE_PREIMAGE) == f1
        assert lock_key(f1) == lock_key(f1)

    print("\n=== All cross-checks passed ===")


if __name__ == "__main__":
    main()
