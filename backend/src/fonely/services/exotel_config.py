"""Exotel virtual number to business mapping — strict startup validation."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("fonely.services.exotel_config")


class InvalidNumberMappingError(ValueError):
    """Number mapping fails startup validation."""


class ExotelNumberMapping:
    """Maps Exotel virtual numbers to business_id.

    Validates at construction: JSON object, string keys, positive int values.
    Invalid input raises InvalidNumberMappingError.
    """

    def __init__(self, mappings: dict[str, int] | None = None) -> None:
        if mappings is not None:
            self._mappings = self._validate(mappings)
        else:
            self._mappings = {}

    @staticmethod
    def from_json(raw: str) -> ExotelNumberMapping:
        """Parse and validate from JSON string (environment variable)."""
        if not raw or not raw.strip():
            return ExotelNumberMapping({})
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidNumberMappingError(
                "EXOTEL_NUMBER_MAPPINGS is not valid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise InvalidNumberMappingError(
                f"EXOTEL_NUMBER_MAPPINGS must be a JSON object, got {type(parsed).__name__}"
            )
        return ExotelNumberMapping(parsed)

    @staticmethod
    def _validate(mappings: dict[str, object]) -> dict[str, int]:
        validated: dict[str, int] = {}
        for key, value in mappings.items():
            if not isinstance(key, str) or not key:
                raise InvalidNumberMappingError(
                    "mapping key must be a non-empty string"
                )
            if isinstance(value, bool):
                raise InvalidNumberMappingError(
                    "mapping value must be a positive integer, got bool"
                )
            if not isinstance(value, int):
                raise InvalidNumberMappingError(
                    f"mapping value must be a positive integer, got {type(value).__name__}"
                )
            if value <= 0:
                raise InvalidNumberMappingError(
                    f"mapping value must be positive, got {value}"
                )
            validated[key] = value
        return validated

    def get_business_id(self, exotel_number: str) -> int | None:
        return self._mappings.get(exotel_number)

    def is_empty(self) -> bool:
        return len(self._mappings) == 0
