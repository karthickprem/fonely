"""WhatsApp business phone mapping configuration."""

import json
import logging

from fonely.core.config import settings

logger = logging.getLogger("fonely.services.whatsapp_config")


class WhatsAppBusinessMapping:
    def __init__(self, mappings: dict[str, int] | None = None) -> None:
        if mappings is not None:
            self._mappings = mappings
        elif settings.whatsapp_business_mappings:
            try:
                self._mappings = json.loads(settings.whatsapp_business_mappings)
            except json.JSONDecodeError:
                logger.error("invalid_whatsapp_business_mappings")
                self._mappings = {}
        else:
            self._mappings = {}

    def get_business_id(self, phone_number_id: str) -> int | None:
        return self._mappings.get(phone_number_id)

    def get_phone_number_id(self, business_id: int) -> str | None:
        matches = [
            phone_number_id
            for phone_number_id, mapped_business_id in self._mappings.items()
            if mapped_business_id == business_id
        ]
        if len(matches) != 1:
            return None
        return matches[0]
