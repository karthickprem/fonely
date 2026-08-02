"""LLM-based structured fact extraction from Tanglish/Tamil/English input."""

import json
import logging
from dataclasses import dataclass, field

from fonely.services.conversation_tools import BusinessContext
from fonely.services.model_gateway import ModelGateway

logger = logging.getLogger("fonely.services.fact_extractor")

_EXTRACTION_SYSTEM_PROMPT = """You extract structured booking facts from customer messages.
The customer may speak in Tamil (Romanized/Tanglish), Tamil script, English, or mixed.

Return ONLY valid JSON with these fields:
{{
  "intent": "book|cancel|reschedule|check|ask_hours|ask_fees|medical|unknown",
  "patient_name": "string or null",
  "service_query": "what the caller said about the service, or null",
  "service_match": "closest matching service name from the list below, or null",
  "doctor_query": "what the caller said about the doctor, or null",
  "doctor_match": "closest matching doctor name from the list below, or null",
  "date_expression": "raw date text (e.g. naalaikku, tomorrow, Saturday) or null",
  "time_expression": "raw time text (e.g. maalai, 6:30, aaru mani) or null",
  "phone": "phone digits only, or null",
  "symptoms": [],
  "confidence": 0.0-1.0,
  "ambiguities": []
}}

Common Tamil/Tanglish patterns:
- "naalaikku" = tomorrow, "innikku" = today
- "kaalaila" = morning, "maalai" = evening
- "aaru mani" = 6 o'clock, "anju mani" = 5 o'clock
- "en peru X" = my name is X
- "pallu vali" = tooth pain
- "scaling pannikanum" = need scaling
- "yaaraavadhu doctor" = any doctor
- "appointment venum" = need appointment
- Tamil numerals: oru=1, rendu=2, moonu=3, naalu=4, anju=5,
  aaru=6, yezhu=7, ettu=8, ombadhu=9, pathu=10

Available services: {services}
Available doctors: {doctors}

Do NOT invent services or doctors not in the lists above.
Return ONLY the JSON object, no other text."""


@dataclass(frozen=True)
class ExtractedFacts:
    intent: str | None = None
    patient_name: str | None = None
    service_query: str | None = None
    service_match: str | None = None
    doctor_query: str | None = None
    doctor_match: str | None = None
    date_expression: str | None = None
    time_expression: str | None = None
    phone: str | None = None
    symptoms: list[str] = field(default_factory=list)
    confidence: float = 0.0
    ambiguities: list[str] = field(default_factory=list)


class FactExtractor:
    def __init__(self, model: ModelGateway) -> None:
        self._model = model

    async def extract(
        self,
        user_message: str,
        business_context: BusinessContext,
        existing_facts: dict[str, object],
    ) -> ExtractedFacts:
        services_text = ", ".join(f"{svc.name} (ID:{svc.id})" for svc in business_context.services)
        doctors_text = ", ".join(f"{res.name} (ID:{res.id})" for res in business_context.resources)
        system = _EXTRACTION_SYSTEM_PROMPT.format(services=services_text, doctors=doctors_text)

        try:
            response = await self._model.complete(
                system_prompt=system,
                messages=[{"role": "user", "content": user_message}],
                temperature=0.1,
                max_tokens=300,
            )
            return self._parse_response(response.text)
        except Exception:
            logger.warning("fact_extraction_failed", exc_info=True)
            return ExtractedFacts()

    @staticmethod
    def _parse_response(text: str) -> ExtractedFacts:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(line for line in lines if not line.strip().startswith("```"))

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("fact_extraction_json_error", extra={"text": cleaned[:200]})
            return ExtractedFacts()

        if not isinstance(data, dict):
            return ExtractedFacts()

        return ExtractedFacts(
            intent=data.get("intent"),
            patient_name=data.get("patient_name"),
            service_query=data.get("service_query"),
            service_match=data.get("service_match"),
            doctor_query=data.get("doctor_query"),
            doctor_match=data.get("doctor_match"),
            date_expression=data.get("date_expression"),
            time_expression=data.get("time_expression"),
            phone=data.get("phone"),
            symptoms=data.get("symptoms", []),
            confidence=float(data.get("confidence", 0.0)),
            ambiguities=data.get("ambiguities", []),
        )
