"""Tests for Tanglish/Tamil/English fact extraction and resolution."""

import json
from datetime import time, timedelta
from unittest.mock import AsyncMock

from fonely.services.conversation_tools import BusinessContext, ResourceInfo, ServiceInfo
from fonely.services.fact_extractor import ExtractedFacts, FactExtractor
from fonely.services.fact_resolver import FactResolver, ResolvedFacts
from fonely.services.model_gateway import ModelResponse


def _dental_clinic() -> BusinessContext:
    return BusinessContext(
        business_id=1,
        name="Smile Dental",
        timezone="Asia/Kolkata",
        services=[
            ServiceInfo(1, "General Consultation", 20, 0, 10, "300"),
            ServiceInfo(2, "Root Canal", 60, 0, 15, "3500"),
            ServiceInfo(3, "Scaling & Polishing", 30, 0, 10, "800"),
            ServiceInfo(4, "Tooth Extraction", 30, 0, 15, "500"),
            ServiceInfo(5, "Orthodontic Consultation", 30, 0, 10, "500"),
        ],
        resources=[
            ResourceInfo(id=1, name="Dr. Priya Krishnan", resource_type="staff"),
            ResourceInfo(id=2, name="Dr. Arjun Venkatesh", resource_type="staff"),
        ],
        eligibility=[(1, 1), (1, 2), (2, 1), (3, 1), (4, 1), (5, 2)],
    )


def _mock_gateway(response_json: dict) -> AsyncMock:
    gateway = AsyncMock()
    gateway.complete.return_value = ModelResponse(text=json.dumps(response_json))
    return gateway


class TestFactExtractor:
    async def test_tanglish_booking_extraction(self) -> None:
        gateway = _mock_gateway(
            {
                "intent": "book",
                "service_query": "scaling",
                "service_match": "Scaling & Polishing",
                "date_expression": "naalaikku",
                "time_expression": "maalai aaru mani",
                "confidence": 0.9,
            }
        )
        extractor = FactExtractor(gateway)
        result = await extractor.extract(
            "naalaikku maalai aaru mani scaling appointment",
            _dental_clinic(),
            {},
        )
        assert result.intent == "book"
        assert result.service_match == "Scaling & Polishing"
        assert result.date_expression == "naalaikku"
        assert result.time_expression == "maalai aaru mani"

    async def test_tamil_name_extraction(self) -> None:
        gateway = _mock_gateway(
            {
                "intent": "book",
                "patient_name": "Karthik",
                "symptoms": ["tooth pain"],
                "confidence": 0.85,
            }
        )
        extractor = FactExtractor(gateway)
        result = await extractor.extract("en peru Karthik, pallu vali", _dental_clinic(), {})
        assert result.patient_name == "Karthik"
        assert "tooth pain" in result.symptoms

    async def test_doctor_query_extraction(self) -> None:
        gateway = _mock_gateway(
            {
                "intent": "book",
                "doctor_query": "Priya",
                "doctor_match": "Dr. Priya Krishnan",
                "service_query": "consultation",
                "service_match": "General Consultation",
                "confidence": 0.9,
            }
        )
        extractor = FactExtractor(gateway)
        result = await extractor.extract("Dr. Priya kitta consultation", _dental_clinic(), {})
        assert result.doctor_match == "Dr. Priya Krishnan"
        assert result.service_match == "General Consultation"

    async def test_any_doctor_extraction(self) -> None:
        gateway = _mock_gateway(
            {
                "intent": "book",
                "doctor_query": "any",
                "confidence": 0.8,
            }
        )
        extractor = FactExtractor(gateway)
        result = await extractor.extract("yaaraavadhu doctor okay", _dental_clinic(), {})
        assert result.doctor_query == "any"

    async def test_gateway_failure_returns_empty(self) -> None:
        gateway = AsyncMock()
        gateway.complete.side_effect = TimeoutError("timeout")
        extractor = FactExtractor(gateway)
        result = await extractor.extract("test message", _dental_clinic(), {})
        assert result.intent is None
        assert result.confidence == 0.0

    async def test_invalid_json_returns_empty(self) -> None:
        gateway = AsyncMock()
        gateway.complete.return_value = ModelResponse(text="not valid json")
        extractor = FactExtractor(gateway)
        result = await extractor.extract("test", _dental_clinic(), {})
        assert result.intent is None


class TestFactResolver:
    def _resolver(self) -> FactResolver:
        return FactResolver()

    def test_service_exact_match(self) -> None:
        extracted = ExtractedFacts(service_match="Scaling & Polishing")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.service_id == 3
        assert resolved.service_name == "Scaling & Polishing"

    def test_service_partial_match(self) -> None:
        extracted = ExtractedFacts(service_query="scaling")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.service_id == 3
        assert resolved.service_name == "Scaling & Polishing"

    def test_service_no_match(self) -> None:
        extracted = ExtractedFacts(service_query="brain surgery")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.service_id is None
        assert any("No service" in a for a in resolved.ambiguities)

    def test_doctor_fuzzy_match(self) -> None:
        extracted = ExtractedFacts(
            doctor_match="Dr. Priya Krishnan", service_match="General Consultation"
        )
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resource_id == 1
        assert resolved.resource_name == "Dr. Priya Krishnan"

    def test_doctor_ineligible(self) -> None:
        extracted = ExtractedFacts(
            service_match="Root Canal",
            doctor_match="Dr. Arjun Venkatesh",
        )
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resource_id is None
        assert any("not available" in a for a in resolved.ambiguities)

    def test_any_doctor(self) -> None:
        extracted = ExtractedFacts(doctor_query="yaaraavadhu")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resource_id is None
        assert not resolved.ambiguities

    def test_date_tomorrow_tanglish(self) -> None:
        extracted = ExtractedFacts(date_expression="naalaikku")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        from datetime import datetime
        from zoneinfo import ZoneInfo

        expected = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
        assert resolved.resolved_date == expected

    def test_date_today_tanglish(self) -> None:
        extracted = ExtractedFacts(date_expression="innikku")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        from datetime import datetime
        from zoneinfo import ZoneInfo

        expected = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        assert resolved.resolved_date == expected

    def test_date_weekday(self) -> None:
        extracted = ExtractedFacts(date_expression="Saturday")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resolved_date is not None
        assert resolved.resolved_date.weekday() == 5

    def test_time_morning_tanglish(self) -> None:
        extracted = ExtractedFacts(time_expression="kaalaila")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resolved_time == time(10, 0)

    def test_time_evening_tanglish(self) -> None:
        extracted = ExtractedFacts(time_expression="maalai")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resolved_time == time(17, 0)

    def test_time_tamil_numeral(self) -> None:
        extracted = ExtractedFacts(time_expression="aaru mani")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resolved_time == time(18, 0)

    def test_time_tamil_numeral_half(self) -> None:
        extracted = ExtractedFacts(time_expression="aaru arai")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resolved_time == time(18, 30)

    def test_time_anju_mani(self) -> None:
        extracted = ExtractedFacts(time_expression="anju mani")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resolved_time == time(17, 0)

    def test_time_standard(self) -> None:
        extracted = ExtractedFacts(time_expression="6:30 pm")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resolved_time == time(18, 30)

    def test_time_night_rejected(self) -> None:
        extracted = ExtractedFacts(time_expression="raathri")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resolved_time is None
        assert any("night" in a.lower() for a in resolved.ambiguities)

    def test_phone_10_digit(self) -> None:
        extracted = ExtractedFacts(phone="9123456789")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.phone == "+919123456789"

    def test_phone_with_91(self) -> None:
        extracted = ExtractedFacts(phone="+919123456789")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.phone == "+919123456789"

    def test_phone_too_short(self) -> None:
        extracted = ExtractedFacts(phone="12345")
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.phone is None

    def test_combined_tanglish(self) -> None:
        extracted = ExtractedFacts(
            intent="book",
            service_match="Scaling & Polishing",
            date_expression="naalaikku",
            time_expression="aaru mani",
            confidence=0.9,
        )
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.service_id == 3
        assert resolved.resolved_time == time(18, 0)
        assert resolved.start_at is not None

    def test_to_dict(self) -> None:
        resolved = ResolvedFacts(
            service_id=3,
            service_name="Scaling",
            patient_name="Karthik",
            phone="+919123456789",
            intent="book",
        )
        d = resolved.to_dict()
        assert d["service_id"] == 3
        assert d["customer_name"] == "Karthik"
        assert d["customer_phone"] == "+919123456789"
        assert d["intent"] == "book"

    def test_english_date_time(self) -> None:
        extracted = ExtractedFacts(
            date_expression="tomorrow",
            time_expression="6:30 pm",
        )
        resolved = self._resolver().resolve(extracted, _dental_clinic(), "Asia/Kolkata")
        assert resolved.resolved_time == time(18, 30)
        assert resolved.resolved_date is not None
        assert resolved.start_at is not None
