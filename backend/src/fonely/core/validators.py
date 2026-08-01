"""Reusable Pydantic validators and annotated types for domain values.

Phone types:
  IndianMobileNumber — owner/manager WhatsApp identity (strict Indian mobile)
  E164PhoneNumber — requires explicit + and valid country code (no local guessing)

Locale types:
  FonelyLocale — canonical application locale (or-IN for Odia)
  Provider codes mapped in fonely.core.locale_mapping.

Money:
  INRAmount — non-negative Decimal, at most 2 decimal places, rejects float
"""

import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BeforeValidator

# --- Phone ---


def _validate_indian_mobile(v: str) -> str:
    """Normalize Indian mobile numbers. Returns +91XXXXXXXXXX."""
    cleaned = re.sub(r"[\s\-\(\)]", "", v)
    if cleaned.startswith("+91"):
        digits = cleaned[3:]
    elif cleaned.startswith("91") and len(cleaned) >= 12:
        digits = cleaned[2:]
    elif cleaned.startswith("0"):
        digits = cleaned[1:]
    else:
        digits = cleaned
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        msg = f"Invalid Indian mobile number: {v}"
        raise ValueError(msg)
    return f"+91{digits}"


def _validate_e164(v: str) -> str:
    """Validate E.164: must start with + followed by country code (1-9) and 6-14 digits."""
    cleaned = re.sub(r"[\s\-\(\)]", "", v)
    if not cleaned.startswith("+"):
        msg = f"E.164 phone must start with +, got: {v}"
        raise ValueError(msg)
    if not re.fullmatch(r"\+[1-9]\d{6,14}", cleaned):
        msg = f"Invalid E.164 phone number: {v}"
        raise ValueError(msg)
    return cleaned


IndianMobileNumber = Annotated[str, AfterValidator(_validate_indian_mobile)]
E164PhoneNumber = Annotated[str, AfterValidator(_validate_e164)]


# --- Locale ---

SUPPORTED_FONELY_LOCALES = frozenset(
    {
        "ta-IN",
        "hi-IN",
        "te-IN",
        "kn-IN",
        "ml-IN",
        "bn-IN",
        "mr-IN",
        "gu-IN",
        "pa-IN",
        "or-IN",
        "en-IN",
        "as-IN",
        "ur-IN",
    }
)


def _validate_fonely_locale(v: str) -> str:
    if v not in SUPPORTED_FONELY_LOCALES:
        msg = f"Unsupported locale: {v}. Supported: {sorted(SUPPORTED_FONELY_LOCALES)}"
        raise ValueError(msg)
    return v


FonelyLocale = Annotated[str, AfterValidator(_validate_fonely_locale)]


# --- Timezone ---


def _validate_iana_timezone(v: str) -> str:
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, KeyError) as exc:
        msg = f"Invalid timezone: {v}"
        raise ValueError(msg) from exc
    return v


IANATimezone = Annotated[str, AfterValidator(_validate_iana_timezone)]


# --- Decimal: domain boundary rejects float ---


def _coerce_strict_decimal(v: object) -> Decimal:
    """Accept Decimal, str, int. Reject bool and float at domain boundary."""
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):
        raise ValueError("Boolean is not a valid decimal value")
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, str):
        try:
            return Decimal(v)
        except InvalidOperation as exc:
            msg = f"Invalid decimal string: {v}"
            raise ValueError(msg) from exc
    if isinstance(v, float):
        msg = f"Float is not accepted for monetary/quantity values. Use Decimal or str. Got: {v}"
        raise ValueError(msg)
    msg = f"Cannot coerce {type(v).__name__} to Decimal"
    raise ValueError(msg)


def _validate_finite(v: Decimal) -> Decimal:
    if not v.is_finite():
        msg = f"Decimal value must be finite, got {v}"
        raise ValueError(msg)
    return v


def _validate_nonnegative(v: Decimal) -> Decimal:
    if v < 0:
        msg = f"Value must be non-negative, got {v}"
        raise ValueError(msg)
    return v


def _validate_positive(v: Decimal) -> Decimal:
    if v <= 0:
        msg = f"Value must be positive, got {v}"
        raise ValueError(msg)
    return v


_TWO_PLACES = Decimal("0.01")


def _validate_two_decimal_places(v: Decimal) -> Decimal:
    """Reject values with more than two decimal places."""
    if v != v.quantize(_TWO_PLACES):
        msg = f"Value must have at most 2 decimal places, got {v}"
        raise ValueError(msg)
    return v


NonNegativeDecimal = Annotated[
    Decimal,
    BeforeValidator(_coerce_strict_decimal),
    AfterValidator(_validate_finite),
    AfterValidator(_validate_nonnegative),
]
PositiveDecimal = Annotated[
    Decimal,
    BeforeValidator(_coerce_strict_decimal),
    AfterValidator(_validate_finite),
    AfterValidator(_validate_positive),
]
Quantity = Annotated[
    Decimal,
    BeforeValidator(_coerce_strict_decimal),
    AfterValidator(_validate_finite),
    AfterValidator(_validate_positive),
    AfterValidator(_validate_two_decimal_places),
]
INRAmount = Annotated[
    Decimal,
    BeforeValidator(_coerce_strict_decimal),
    AfterValidator(_validate_finite),
    AfterValidator(_validate_nonnegative),
    AfterValidator(_validate_two_decimal_places),
]


# --- Date and datetime ---


def _parse_iso_date(v: object) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError as exc:
            msg = f"Invalid ISO date: {v}"
            raise ValueError(msg) from exc
    msg = "Date must be an ISO date string or date object"
    raise ValueError(msg)


def _parse_iso_datetime(v: object) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        normalized = v[:-1] + "+00:00" if v.endswith("Z") else v
        try:
            return datetime.fromisoformat(normalized)
        except ValueError as exc:
            msg = f"Invalid ISO datetime: {v}"
            raise ValueError(msg) from exc
    msg = "Datetime must be an ISO datetime string or datetime object"
    raise ValueError(msg)


def _validate_aware_datetime(v: datetime) -> datetime:
    if v.tzinfo is None:
        msg = "Datetime must be timezone-aware"
        raise ValueError(msg)
    return v


ISODate = Annotated[date, BeforeValidator(_parse_iso_date)]
AwareDatetime = Annotated[
    datetime,
    BeforeValidator(_parse_iso_datetime),
    AfterValidator(_validate_aware_datetime),
]


def utcnow() -> datetime:
    return datetime.now(UTC)


def quantize_inr(amount: Decimal) -> Decimal:
    """Round a Decimal to two places using half-up rounding."""
    if not amount.is_finite():
        msg = f"INR amount must be finite, got {amount}"
        raise ValueError(msg)
    return amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
