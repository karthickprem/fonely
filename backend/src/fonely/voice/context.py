"""Trusted session context: clock, timezone, and authoritative query ports.

Root cause of the R&D date/availability bug: the pipeline had no
trusted datetime injection and relied on static prompt text for
schedule information.  When a user asked "is the doctor free today?",
the LLM could not resolve "today" and fell back to generic hours.

This module injects trusted, immutable temporal context and typed
read-only query ports that the pipeline consumes without domain
mutation.
"""

from __future__ import annotations

import enum
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Protocol


@dataclass(frozen=True)
class TrustedClock:
    """Application-injected trusted current time and business timezone.

    Never derived from model output or caller input.
    """

    now_utc: datetime
    business_timezone: str
    business_date: date
    day_of_week: str

    @classmethod
    def from_now(cls, tz_name: str) -> TrustedClock:
        if not tz_name:
            raise ValueError("business_timezone is required; no hardcoded fallback")
        now = datetime.now(UTC)
        tz = zoneinfo.ZoneInfo(tz_name)
        local = now.astimezone(tz)
        return cls(
            now_utc=now,
            business_timezone=tz_name,
            business_date=local.date(),
            day_of_week=local.strftime("%A").lower(),
        )


TAMIL_RELATIVE_DATES = {
    "இன்று": 0,
    "இன்னைக்கு": 0,
    "இன்னைக்கே": 0,
    "today": 0,
    "innaikku": 0,
    "innaiku": 0,
    "நாளை": 1,
    "நாளைக்கு": 1,
    "tomorrow": 1,
    "naalaikku": 1,
    "naalai": 1,
    "நாளை மறுநாள்": 2,
    "day after tomorrow": 2,
}


def resolve_relative_date(
    text: str,
    clock: TrustedClock,
) -> date | None:
    """Resolve Tamil/Tanglish/English relative date words to an absolute date.

    Returns None if no recognized relative date word is found.
    Uses only the trusted clock, never invents dates.
    """
    import re

    normalized = " ".join(text.casefold().split())
    for phrase, offset in sorted(TAMIL_RELATIVE_DATES.items(), key=lambda x: -len(x[0])):
        pattern = r"(?<!\w)" + re.escape(phrase.casefold()) + r"(?!\w)"
        if re.search(pattern, normalized):
            from datetime import timedelta

            return clock.business_date + timedelta(days=offset)
    return None


class SlotStatus(enum.StrEnum):
    AVAILABLE = "available"
    BOOKED = "booked"
    BLOCKED = "blocked"
    LEAVE = "leave"


@dataclass(frozen=True)
class AvailableSlot:
    resource_id: int
    resource_name: str
    start_time: time
    end_time: time
    service_name: str
    status: SlotStatus = SlotStatus.AVAILABLE


@dataclass(frozen=True)
class DayAvailability:
    business_date: date
    day_of_week: str
    is_operating_day: bool
    is_exception_day: bool
    operating_hours: tuple[tuple[time, time], ...] = ()
    available_slots: tuple[AvailableSlot, ...] = ()
    fully_booked: bool = False
    reason: str = ""


@dataclass(frozen=True)
class AvailabilityQuery:
    """Typed availability query scoped by trusted business context."""

    business_id: int
    target_date: date
    business_timezone: str
    service_id: int | None = None
    resource_id: int | None = None
    capability: str | None = None


class AvailabilityPort(Protocol):
    """Read-only authoritative availability query.

    Scoped by trusted business_id, service, resource, and date.
    Never mutates state.  Returns typed DayAvailability with
    service-specific slots filtered by status.
    """

    async def query_day_availability(
        self,
        query: AvailabilityQuery,
    ) -> DayAvailability: ...


class StubAvailabilityPort:
    """Fail-closed stub: returns no-data availability.

    Production implementation will query the authoritative backend
    AppointmentService/InventoryService for real slot/stock data.
    """

    async def query_day_availability(
        self,
        query: AvailabilityQuery,
    ) -> DayAvailability:
        return DayAvailability(
            business_date=query.target_date,
            day_of_week=query.target_date.strftime("%A").lower(),
            is_operating_day=False,
            is_exception_day=False,
            reason="availability data not connected",
        )
