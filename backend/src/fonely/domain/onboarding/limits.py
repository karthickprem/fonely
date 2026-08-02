"""Centralized practical draft limits for safe validation."""

import re

MAX_LOCATIONS = 20
MAX_SERVICES = 100
MAX_PRODUCTS = 500
MAX_RESOURCES = 50
MAX_SCHEDULE_PERIODS = 21
MAX_SCHEDULE_EXCEPTIONS = 365
MAX_PROVENANCE_ENTRIES = 10
MAX_PROVENANCE_PATHS = 200
MAX_ISSUES = 500
MAX_QUESTIONS = 200
MAX_SHORT_TEXT = 200
MAX_LONG_TEXT = 2000
MAX_LANGUAGES = 13
MAX_LOCATION_KEYS_PER_ENTITY = 20
MAX_RESOURCE_KEYS_PER_SERVICE = 50
MAX_SERVICE_KEYS_PER_RESOURCE = 100
MAX_SOURCE_BATCHES = 50
MAX_REVIEWER_REF = 200
MAX_KEY_LENGTH = 100

SCHEMA_VERSION = 1

SUPPORTED_CURRENCIES = frozenset({"INR"})

_KEY_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def validate_key_element(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field}: key must not be empty or whitespace")
    if len(stripped) > MAX_KEY_LENGTH:
        raise ValueError(f"{field}: key exceeds {MAX_KEY_LENGTH} characters")
    if not _KEY_RE.fullmatch(stripped):
        raise ValueError(f"{field}: key contains invalid characters: {stripped!r}")
    return stripped


def normalize_currency(value: str) -> str:
    upper = value.strip().upper()
    if not upper or len(upper) != 3 or upper not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Unsupported currency: {value!r}")
    return upper
