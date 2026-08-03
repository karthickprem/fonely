"""LLM-based owner command parsing from natural Tanglish/Tamil/English input."""

import json
import logging
from dataclasses import dataclass

from fonely.services.model_gateway import ModelGateway

logger = logging.getLogger("fonely.services.owner_command_parser")

_PARSER_SYSTEM_PROMPT = """You parse a clinic owner's WhatsApp message into a structured command.
The owner manages a dental/medical clinic. Parse their message into JSON.

Commands (return ONE JSON object):
- doctor_leave: doctor_name, date, reason
- close_clinic: date, reason
- close_early: date, close_time (HH:MM)
- add_offer: description, valid_until
- cancel_appointment: patient_name, date
- get_summary: date (today or tomorrow)
- add_note: note, for_date
- unknown: when command is unclear

Common Tamil/Tanglish:
- "sick leave" / "leave" → doctor_leave
- "clinic closed" / "clinic mooduvom" → close_clinic
- "appointments kaatu" / "show appointments" → get_summary
- "naalai" = tomorrow, "innikku" = today

Clinic doctors: {doctors}

Owner message: "{message}"

Return ONLY valid JSON."""


@dataclass(frozen=True)
class ParsedOwnerCommand:
    command: str
    doctor_name: str | None = None
    date: str | None = None
    reason: str | None = None
    close_time: str | None = None
    description: str | None = None
    valid_until: str | None = None
    patient_name: str | None = None
    note: str | None = None
    for_date: str | None = None


class OwnerCommandParser:
    def __init__(self, model: ModelGateway) -> None:
        self._model = model

    async def parse(self, message: str, doctor_names: list[str]) -> ParsedOwnerCommand:
        doctors_text = ", ".join(doctor_names) if doctor_names else "none configured"
        system = _PARSER_SYSTEM_PROMPT.format(doctors=doctors_text, message=message)

        try:
            response = await self._model.complete(
                system_prompt=system,
                messages=[{"role": "user", "content": message}],
                temperature=0.1,
                max_tokens=200,
            )
            return self._parse_response(response.text)
        except Exception:
            logger.warning("owner_command_parse_failed", exc_info=True)
            return ParsedOwnerCommand(command="unknown")

    @staticmethod
    def _parse_response(text: str) -> ParsedOwnerCommand:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(line for line in lines if not line.strip().startswith("```"))

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return ParsedOwnerCommand(command="unknown")

        if not isinstance(data, dict):
            return ParsedOwnerCommand(command="unknown")

        return ParsedOwnerCommand(
            command=data.get("command", "unknown"),
            doctor_name=data.get("doctor_name"),
            date=data.get("date"),
            reason=data.get("reason"),
            close_time=data.get("close_time"),
            description=data.get("description"),
            valid_until=data.get("valid_until"),
            patient_name=data.get("patient_name"),
            note=data.get("note"),
            for_date=data.get("for_date"),
        )
