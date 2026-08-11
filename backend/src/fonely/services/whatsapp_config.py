"""WhatsApp business phone mapping configuration."""

from __future__ import annotations

import json
import logging

from fonely.core.config import settings

logger = logging.getLogger("fonely.services.whatsapp_config")


def parse_business_mappings(raw: str) -> dict[str, int]:
    if not raw or not raw.strip():
        raise ValueError("WHATSAPP_BUSINESS_MAPPINGS is required but empty")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("WHATSAPP_BUSINESS_MAPPINGS is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("WHATSAPP_BUSINESS_MAPPINGS must be a JSON object")
    if not parsed:
        raise ValueError("WHATSAPP_BUSINESS_MAPPINGS must not be empty")
    result: dict[str, int] = {}
    for key, value in parsed.items():
        str_key = str(key)
        if not str_key:
            raise ValueError("WHATSAPP_BUSINESS_MAPPINGS: empty phone_number_id key")
        if str_key in result:
            raise ValueError(f"WHATSAPP_BUSINESS_MAPPINGS: duplicate key {str_key!r}")
        if not isinstance(value, int) or isinstance(value, bool):
            vtype = type(value).__name__
            raise ValueError(
                f"WHATSAPP_BUSINESS_MAPPINGS: business_id for {str_key!r} "
                f"must be integer, got {vtype}"
            )
        if value <= 0:
            raise ValueError(
                f"WHATSAPP_BUSINESS_MAPPINGS: business_id for {str_key!r} must be positive"
            )
        result[str_key] = value
    return result


class WhatsAppBusinessMapping:
    def __init__(self, mappings: dict[str, int] | None = None) -> None:
        if mappings is not None:
            self._mappings = mappings
        elif settings.whatsapp_business_mappings:
            self._mappings = parse_business_mappings(settings.whatsapp_business_mappings)
        else:
            self._mappings = {}

    def get_business_id(self, phone_number_id: str) -> int | None:
        return self._mappings.get(phone_number_id)

    def get_phone_number_id(self, business_id: int, *, preferred: str | None = None) -> str | None:
        matches = [
            phone_number_id
            for phone_number_id, mapped_business_id in self._mappings.items()
            if mapped_business_id == business_id
        ]
        if preferred and preferred in matches:
            return preferred
        if len(matches) != 1:
            return None
        return matches[0]
