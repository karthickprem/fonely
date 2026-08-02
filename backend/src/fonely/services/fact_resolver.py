"""Deterministic fact resolution from LLM-extracted Tanglish/Tamil/English facts."""

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fonely.services.conversation_tools import BusinessContext
from fonely.services.fact_extractor import ExtractedFacts

_TAMIL_NUMERALS: dict[str, int] = {
    "oru": 1,
    "onnu": 1,
    "onna": 1,
    "rendu": 2,
    "renda": 2,
    "moonu": 3,
    "moona": 3,
    "naalu": 4,
    "naala": 4,
    "anju": 5,
    "aaru": 6,
    "aru": 6,
    "yezhu": 7,
    "ezhu": 7,
    "ettu": 8,
    "ombadhu": 9,
    "ombodhu": 9,
    "pathu": 10,
    "paththu": 10,
    "pathinonnu": 11,
    "pathinoru": 11,
    "pannrendu": 12,
}

_TOMORROW_PATTERNS = frozenset(
    {
        "tomorrow",
        "naalaikku",
        "naalai",
        "நாளை",
        "நாளைக்கு",
    }
)
_TODAY_PATTERNS = frozenset(
    {
        "today",
        "innikku",
        "innaiku",
        "இன்று",
        "இன்னைக்கு",
    }
)
_MORNING_PATTERNS = frozenset(
    {
        "morning",
        "kaalaila",
        "kaalai",
        "காலை",
        "காலையில",
    }
)
_EVENING_PATTERNS = frozenset(
    {
        "evening",
        "maalai",
        "மாலை",
        "சாயங்காலம்",
    }
)
_NIGHT_PATTERNS = frozenset(
    {
        "night",
        "raathri",
        "raatri",
        "இரவு",
    }
)

_WEEKDAY_MAP: dict[str, int] = {
    "monday": 0,
    "thingal": 0,
    "திங்கள்": 0,
    "tuesday": 1,
    "sevvai": 1,
    "செவ்வாய்": 1,
    "wednesday": 2,
    "budhan": 2,
    "புதன்": 2,
    "thursday": 3,
    "viyazhan": 3,
    "வியாழன்": 3,
    "friday": 4,
    "velli": 4,
    "வெள்ளி": 4,
    "saturday": 5,
    "sani": 5,
    "சனி": 5,
    "sunday": 6,
    "nyayiru": 6,
    "ஞாயிறு": 6,
}

_ANY_DOCTOR_PATTERNS = frozenset(
    {
        "any",
        "anyone",
        "anybody",
        "yaaraavadhu",
        "yaravadhu",
        "yaaraavathu",
        "எந்த",
        "யாராவது",
    }
)


@dataclass(frozen=True)
class ResolvedFacts:
    service_id: int | None = None
    service_name: str | None = None
    resource_id: int | None = None
    resource_name: str | None = None
    resolved_date: date | None = None
    resolved_time: time | None = None
    start_at: datetime | None = None
    patient_name: str | None = None
    phone: str | None = None
    intent: str | None = None
    symptoms: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.intent:
            result["intent"] = self.intent
        if self.service_id is not None:
            result["service_id"] = self.service_id
            result["service_name"] = self.service_name
        if self.resource_id is not None:
            result["resource_id"] = self.resource_id
            result["resource_name"] = self.resource_name
        if self.start_at is not None:
            result["start_at"] = self.start_at
        if self.patient_name:
            result["customer_name"] = self.patient_name
        if self.phone:
            result["customer_phone"] = self.phone
        return result


class FactResolver:
    def resolve(
        self,
        extracted: ExtractedFacts,
        business_context: BusinessContext,
        clinic_timezone: str,
    ) -> ResolvedFacts:
        service_id, service_name, svc_ambiguity = self._resolve_service(extracted, business_context)
        resource_id, resource_name, res_ambiguity = self._resolve_resource(
            extracted, business_context, service_id
        )
        resolved_date = self._resolve_date(extracted.date_expression, clinic_timezone)
        resolved_time = self._resolve_time(extracted.time_expression)
        start_at = self._combine_datetime(resolved_date, resolved_time, clinic_timezone)
        phone = self._resolve_phone(extracted.phone)
        name = extracted.patient_name.strip() if extracted.patient_name else None

        ambiguities = list(extracted.ambiguities)
        if svc_ambiguity:
            ambiguities.append(svc_ambiguity)
        if res_ambiguity:
            ambiguities.append(res_ambiguity)
        if resolved_time is None and extracted.time_expression:
            for p in _NIGHT_PATTERNS:
                if p in (extracted.time_expression or "").lower():
                    ambiguities.append("Clinic is closed at night")
                    break

        return ResolvedFacts(
            service_id=service_id,
            service_name=service_name,
            resource_id=resource_id,
            resource_name=resource_name,
            resolved_date=resolved_date,
            resolved_time=resolved_time,
            start_at=start_at,
            patient_name=name,
            phone=phone,
            intent=extracted.intent,
            symptoms=extracted.symptoms,
            ambiguities=ambiguities,
        )

    def _resolve_service(
        self, extracted: ExtractedFacts, biz: BusinessContext
    ) -> tuple[int | None, str | None, str | None]:
        match_name = extracted.service_match or extracted.service_query
        if not match_name:
            return None, None, None

        match_lower = match_name.lower()
        for svc in biz.services:
            if svc.name.lower() == match_lower:
                return svc.id, svc.name, None

        matches = [svc for svc in biz.services if match_lower in svc.name.lower()]
        if len(matches) == 1:
            return matches[0].id, matches[0].name, None
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches)
            return None, None, f"Multiple services match '{match_name}': {names}"

        for svc in biz.services:
            if svc.name.lower() in match_lower:
                return svc.id, svc.name, None

        return None, None, f"No service matches '{match_name}'"

    def _resolve_resource(
        self,
        extracted: ExtractedFacts,
        biz: BusinessContext,
        service_id: int | None,
    ) -> tuple[int | None, str | None, str | None]:
        query = extracted.doctor_match or extracted.doctor_query
        if not query:
            return None, None, None

        query_lower = query.lower().strip()
        if query_lower in _ANY_DOCTOR_PATTERNS:
            return None, None, None

        for res in biz.resources:
            if res.name.lower() == query_lower or query_lower in res.name.lower():
                if service_id is not None:
                    eligible = any(
                        sid == service_id and rid == res.id for sid, rid in biz.eligibility
                    )
                    if not eligible:
                        return None, None, (f"{res.name} is not available for this service")
                return res.id, res.name, None

        return None, None, f"No doctor matches '{query}'"

    def _resolve_date(self, expr: str | None, timezone: str) -> date | None:
        if not expr:
            return None

        expr_lower = expr.lower().strip()
        tz = ZoneInfo(timezone)
        today = datetime.now(tz).date()

        for pattern in _TODAY_PATTERNS:
            if pattern in expr_lower:
                return today

        for pattern in _TOMORROW_PATTERNS:
            if pattern in expr_lower:
                return today + timedelta(days=1)

        for day_name, weekday in _WEEKDAY_MAP.items():
            if day_name in expr_lower:
                days_ahead = (weekday - today.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                return today + timedelta(days=days_ahead)

        day_match = re.search(r"(\d{1,2})(?:st|nd|rd|th)?", expr_lower)
        if day_match:
            day = int(day_match.group(1))
            if 1 <= day <= 31:
                try:
                    target = today.replace(day=day)
                    if target <= today:
                        month = today.month + 1
                        year = today.year
                        if month > 12:
                            month = 1
                            year += 1
                        target = today.replace(year=year, month=month, day=day)
                    return target
                except ValueError:
                    pass

        return None

    def _resolve_time(self, expr: str | None) -> time | None:
        if not expr:
            return None

        expr_lower = expr.lower().strip()

        for pattern in _MORNING_PATTERNS:
            if pattern in expr_lower:
                return time(10, 0)

        for pattern in _EVENING_PATTERNS:
            if pattern in expr_lower:
                return time(17, 0)

        for pattern in _NIGHT_PATTERNS:
            if pattern in expr_lower:
                return None

        standard = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", expr_lower)
        if standard:
            hour = int(standard.group(1))
            minute = int(standard.group(2))
            ampm = standard.group(3)
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return time(hour, minute)

        for tamil_word, value in _TAMIL_NUMERALS.items():
            if tamil_word in expr_lower:
                hour = value
                if hour <= 8:
                    hour += 12
                half = "arai" in expr_lower or "half" in expr_lower
                return time(hour, 30 if half else 0)

        hour_match = re.search(r"(\d{1,2})\s*(am|pm|mani|o'clock)", expr_lower)
        if hour_match:
            hour = int(hour_match.group(1))
            suffix = hour_match.group(2)
            if suffix == "pm" and hour < 12:
                hour += 12
            elif suffix == "am" and hour == 12:
                hour = 0
            elif suffix in ("mani", "o'clock") and hour <= 8:
                hour += 12
            if 0 <= hour <= 23:
                return time(hour, 0)

        return None

    def _combine_datetime(
        self,
        resolved_date: date | None,
        resolved_time: time | None,
        timezone: str,
    ) -> datetime | None:
        if resolved_date is None and resolved_time is None:
            return None

        tz = ZoneInfo(timezone)
        target_date = resolved_date or datetime.now(tz).date()
        target_time = resolved_time or time(10, 0)

        local_dt = datetime.combine(target_date, target_time, tzinfo=tz)
        return local_dt.astimezone(UTC)

    def _resolve_phone(self, phone: str | None) -> str | None:
        if not phone:
            return None
        digits = re.sub(r"[^\d+]", "", phone)
        if digits.startswith("+91") and len(digits) >= 13:
            return digits
        if len(digits) == 10:
            return f"+91{digits}"
        if len(digits) == 12 and digits.startswith("91"):
            return f"+{digits}"
        return None
