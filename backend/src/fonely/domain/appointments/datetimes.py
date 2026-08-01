"""Instant-safe datetime operations for appointment domain rules."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def require_aware(value: datetime, *, label: str = "Datetime") -> datetime:
    """Return an aware datetime or reject naive and invalid tzinfo values."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def instant(value: datetime) -> datetime:
    """Normalize an aware datetime to the UTC instant used for comparisons."""
    return require_aware(value).astimezone(UTC)


def add_elapsed(value: datetime, delta: timedelta) -> datetime:
    """Add elapsed time without applying wall-clock arithmetic across DST changes."""
    timezone = require_aware(value).tzinfo
    assert timezone is not None
    return (instant(value) + delta).astimezone(timezone)


def validate_business_local(value: datetime, timezone: str, *, label: str) -> None:
    """Reject invalid local wall representations while accepting canonical UTC instants."""
    aware = require_aware(value, label=label)
    zone = ZoneInfo(timezone)
    if aware.tzinfo is UTC:
        return
    local = aware.astimezone(zone)
    if (
        aware.replace(tzinfo=None) != local.replace(tzinfo=None)
        or aware.utcoffset() != local.utcoffset()
    ):
        raise ValueError(f"{label} does not match business timezone offset and local date")
    if isinstance(aware.tzinfo, ZoneInfo) and aware.tzinfo.key == timezone:
        round_trip = instant(aware).astimezone(zone)
        if round_trip.replace(tzinfo=None) != aware.replace(tzinfo=None):
            raise ValueError(f"{label} does not exist in the business timezone")
        alternate = aware.replace(fold=1 - aware.fold)
        if instant(alternate) != instant(aware) and alternate.utcoffset() != aware.utcoffset():
            raise ValueError(f"{label} is an ambiguous business local time")


def is_before(left: datetime, right: datetime) -> bool:
    return instant(left) < instant(right)


def is_before_or_equal(left: datetime, right: datetime) -> bool:
    return instant(left) <= instant(right)
