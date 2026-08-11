"""Typed application contract for dental booking offers.

AvailabilityOffer and SelectedSlot are consumed by ConversationService
and BookingOrchestrator. Other contract types will be added when they
have production callers.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AvailabilitySlot:
    """One bookable slot within an offer."""

    token: str
    start_at_utc: datetime
    end_at_utc: datetime
    display_date: str
    display_time: str
    display_end_time: str


@dataclass(frozen=True)
class AvailabilityOffer:
    """Authoritative set of offered slots from AvailabilityService.

    Bound to a specific business/conversation/service/resource/date.
    The offer_id and revision prevent stale/tampered selection.
    """

    offer_id: str
    revision: int
    business_id: int
    conversation_id: str
    service_id: int
    service_name: str
    resource_id: int
    resource_name: str
    target_date: str
    slots: tuple[AvailabilitySlot, ...]
    created_at: datetime
    expires_at: datetime

    @staticmethod
    def generate_token(
        offer_id: str,
        slot_start: datetime,
        slot_end: datetime,
        resource_id: int,
        service_id: int,
        expires_at: datetime,
        *,
        secret: str = "fonely-offer-key",
    ) -> str:
        import hmac as _hmac

        raw = (
            f"{offer_id}:{slot_start.isoformat()}:{slot_end.isoformat()}"
            f":{resource_id}:{service_id}:{expires_at.isoformat()}"
        )
        return _hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]

    @staticmethod
    def new_offer_id() -> str:
        return uuid.uuid4().hex[:12]

    def find_by_token(self, token: str) -> AvailabilitySlot | None:
        for slot in self.slots:
            if slot.token == token:
                return slot
        return None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True)
class SelectedSlot:
    """A validated selection from an active offer."""

    offer_id: str
    offer_revision: int
    token: str
    start_at_utc: datetime
    end_at_utc: datetime
    service_id: int
    resource_id: int
