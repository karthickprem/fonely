"""Maps canonical Fonely locales to provider-specific codes.

Fonely uses canonical BCP-47 codes internally (e.g. or-IN for Odia).
Providers like Sarvam may use different codes (e.g. od-IN for Odia).
"""

SARVAM_LOCALE_MAP: dict[str, str] = {
    "ta-IN": "ta-IN",
    "hi-IN": "hi-IN",
    "te-IN": "te-IN",
    "kn-IN": "kn-IN",
    "ml-IN": "ml-IN",
    "bn-IN": "bn-IN",
    "mr-IN": "mr-IN",
    "gu-IN": "gu-IN",
    "pa-IN": "pa-IN",
    "or-IN": "od-IN",  # Sarvam uses od-IN for Odia
    "en-IN": "en-IN",
    "as-IN": "as-IN",
    "ur-IN": "ur-IN",
}


def to_sarvam_locale(fonely_locale: str) -> str:
    """Convert canonical Fonely locale to Sarvam provider code."""
    result = SARVAM_LOCALE_MAP.get(fonely_locale)
    if result is None:
        msg = f"No Sarvam mapping for locale: {fonely_locale}"
        raise ValueError(msg)
    return result
