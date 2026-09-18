"""Semantic schema validator for command family preimages.

Uses ZoneInfo and date.fromisoformat — not regex alone.
Run: python3 docs/contract/schema_validator.py
"""

from datetime import date
from zoneinfo import ZoneInfo
import re

COMMANDS = {
    "doctor_leave": {"family_schema", "business_id", "owner_user_id", "command_type", "target_date", "target_timezone", "resource_id", "reason"},
    "close_clinic": {"family_schema", "business_id", "owner_user_id", "command_type", "target_date", "target_timezone", "reason"},
    "close_early": {"family_schema", "business_id", "owner_user_id", "command_type", "target_date", "target_timezone", "close_time", "reason"},
}

HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def validate(preimage: dict) -> list[str]:
    errors = []
    ct = preimage.get("command_type")
    if ct not in COMMANDS:
        return [f"unknown command_type: {ct}"]
    expected = COMMANDS[ct]
    actual = set(preimage.keys())
    if actual != expected:
        extra = actual - expected
        missing = expected - actual
        if extra:
            errors.append(f"extra fields: {extra}")
        if missing:
            errors.append(f"missing fields: {missing}")
        return errors
    if preimage.get("family_schema") != "owner-command-family-v1":
        errors.append("family_schema must be 'owner-command-family-v1'")
    if not isinstance(preimage.get("business_id"), int) or preimage["business_id"] < 1:
        errors.append("business_id must be int >= 1")
    if not isinstance(preimage.get("owner_user_id"), int) or preimage["owner_user_id"] < 1:
        errors.append("owner_user_id must be int >= 1")
    td = preimage.get("target_date")
    try:
        date.fromisoformat(td)
    except (TypeError, ValueError):
        errors.append(f"target_date invalid ISO date: {td!r}")
    tz = preimage.get("target_timezone")
    try:
        ZoneInfo(tz)
    except (TypeError, KeyError):
        errors.append(f"target_timezone invalid IANA zone: {tz!r}")
    if ct == "doctor_leave":
        if not isinstance(preimage.get("resource_id"), int) or preimage["resource_id"] < 1:
            errors.append("resource_id must be int >= 1")
    if ct == "close_early":
        ctime = preimage.get("close_time")
        if not isinstance(ctime, str) or not HHMM.match(ctime):
            errors.append(f"close_time must be HH:MM: {ctime!r}")
    reason = preimage.get("reason")
    if not isinstance(reason, str) or len(reason) < 1 or len(reason) > 200:
        errors.append("reason must be string 1-200 chars")
    return errors


# ── Tests ──

def _assert_valid(preimage):
    errs = validate(preimage)
    assert not errs, f"expected valid: {errs}"

def _assert_invalid(preimage, substring):
    errs = validate(preimage)
    assert errs, f"expected invalid for {substring}"
    assert any(substring in e for e in errs), f"expected '{substring}' in {errs}"


VALID_DL = {
    "family_schema": "owner-command-family-v1", "business_id": 1, "owner_user_id": 10,
    "command_type": "doctor_leave", "target_date": "2026-08-12",
    "target_timezone": "Asia/Kolkata", "resource_id": 1, "reason": "Leave",
}
VALID_CC = {
    "family_schema": "owner-command-family-v1", "business_id": 1, "owner_user_id": 10,
    "command_type": "close_clinic", "target_date": "2026-08-12",
    "target_timezone": "Asia/Kolkata", "reason": "Holiday",
}
VALID_CE = {
    "family_schema": "owner-command-family-v1", "business_id": 1, "owner_user_id": 10,
    "command_type": "close_early", "target_date": "2026-08-12",
    "target_timezone": "Asia/Kolkata", "close_time": "17:00", "reason": "Closing early",
}

tests_passed = 0

def t(name, fn):
    global tests_passed
    fn()
    tests_passed += 1
    print(f"  {name}: PASS")

t("valid doctor_leave", lambda: _assert_valid(VALID_DL))
t("valid close_clinic", lambda: _assert_valid(VALID_CC))
t("valid close_early", lambda: _assert_valid(VALID_CE))

# Invalid timezone
t("invalid timezone", lambda: _assert_invalid({**VALID_DL, "target_timezone": "Fake/Zone"}, "timezone"))

# Impossible date
t("impossible date Feb 30", lambda: _assert_invalid({**VALID_DL, "target_date": "2026-02-30"}, "target_date"))
t("impossible date month 13", lambda: _assert_invalid({**VALID_DL, "target_date": "2026-13-01"}, "target_date"))
t("non-date string", lambda: _assert_invalid({**VALID_DL, "target_date": "tomorrow"}, "target_date"))

# Extra field
t("extra field forbidden", lambda: _assert_invalid({**VALID_DL, "extra": "bad"}, "extra"))

# Missing field
t("missing resource_id", lambda: _assert_invalid({k: v for k, v in VALID_DL.items() if k != "resource_id"}, "missing"))

# Wrong command fields
t("resource_id on close_clinic", lambda: _assert_invalid({**VALID_CC, "resource_id": 1}, "extra"))
t("close_time on doctor_leave", lambda: _assert_invalid({**VALID_DL, "close_time": "17:00"}, "extra"))

# Invalid close_time
t("close_time 24:00", lambda: _assert_invalid({**VALID_CE, "close_time": "24:00"}, "close_time"))
t("close_time 12:60", lambda: _assert_invalid({**VALID_CE, "close_time": "12:60"}, "close_time"))
t("close_time 1:30", lambda: _assert_invalid({**VALID_CE, "close_time": "1:30"}, "close_time"))

# Bad business_id
t("business_id 0", lambda: _assert_invalid({**VALID_DL, "business_id": 0}, "business_id"))
t("business_id string", lambda: _assert_invalid({**VALID_DL, "business_id": "1"}, "business_id"))

# Bad reason
t("empty reason", lambda: _assert_invalid({**VALID_DL, "reason": ""}, "reason"))
t("reason too long", lambda: _assert_invalid({**VALID_DL, "reason": "x" * 201}, "reason"))

# Wrong schema
t("wrong family_schema", lambda: _assert_invalid({**VALID_DL, "family_schema": "v2"}, "family_schema"))

print(f"\n=== All {tests_passed} schema tests passed ===")
