"""Exotel admission — gateway auth, tenant routing, correlation.

Shared admission for both callback and media/start paths.
Auth before body. Quarantine only after auth. Correlation doesn't
replace semantic dedup.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fonely.services.exotel_config import ExotelNumberMapping

logger = logging.getLogger("fonely.api.channels.exotel_admission")

_AUTH_HEADER = "X-Exotel-Webhook-Secret"
_MIN_SECRET_CHARS = 32


def verify_gateway_secret(
    headers: dict[str, str] | Any,
    configured_secret: str,
) -> bool:
    """Constant-time secret verification. Returns False on any anomaly.

    Checks:
    - Exactly one auth header (duplicate → reject)
    - Non-empty, ASCII-only, no whitespace padding
    - Constant-time comparison
    """
    secret_bytes = _ascii_secret(configured_secret)
    if secret_bytes is None:
        return False

    if hasattr(headers, "getlist"):
        raw_values = headers.getlist(_AUTH_HEADER)
    else:
        val = headers.get(_AUTH_HEADER)
        raw_values = [val] if val else []

    if len(raw_values) != 1:
        return False

    provided = raw_values[0]
    if not provided or provided != provided.strip():
        return False

    try:
        provided_bytes = provided.encode("ascii")
    except UnicodeEncodeError:
        return False

    return hmac.compare_digest(secret_bytes, provided_bytes)


def is_secret_strong(secret: str) -> bool:
    return _ascii_secret(secret) is not None


def _ascii_secret(secret: str) -> bytes | None:
    if len(secret) < _MIN_SECRET_CHARS or secret != secret.strip():
        return None
    try:
        return secret.encode("ascii")
    except UnicodeEncodeError:
        return None


def resolve_business_id(
    mapping: ExotelNumberMapping,
    called: str,
    caller: str,
) -> int | None:
    """Direction-neutral ambiguity-rejecting tenant routing.

    Returns business_id or None if unknown/ambiguous.
    """
    to_bid = mapping.get_business_id(called)
    from_bid = mapping.get_business_id(caller)
    if to_bid is not None and from_bid is not None and to_bid != from_bid:
        return None
    return to_bid or from_bid
