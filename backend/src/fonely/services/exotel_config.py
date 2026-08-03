"""Exotel virtual number to business mapping configuration."""

import json
import logging

from fonely.core.config import settings

logger = logging.getLogger("fonely.services.exotel_config")


class ExotelNumberMapping:
    """Maps Exotel virtual numbers to business_id."""

    def __init__(self, mappings: dict[str, int] | None = None) -> None:
        if mappings is not None:
            self._mappings = mappings
        elif settings.exotel_number_mappings:
            try:
                self._mappings = json.loads(settings.exotel_number_mappings)
            except json.JSONDecodeError:
                logger.error("invalid_exotel_number_mappings")
                self._mappings = {}
        else:
            self._mappings = {}

    def get_business_id(self, exotel_number: str) -> int | None:
        return self._mappings.get(exotel_number)
