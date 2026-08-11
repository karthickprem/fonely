"""Configurable data retention policies for Fonely."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RetentionPolicy:
    data_type: str
    retention_days: int
    description: str


def _env_days(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
        return val if val > 0 else default
    except ValueError:
        return default


def get_retention_policies() -> dict[str, RetentionPolicy]:
    return {
        "conversations": RetentionPolicy(
            data_type="conversations",
            retention_days=_env_days("RETENTION_CONVERSATIONS_DAYS", 90),
            description="Booking conversations with message hashes",
        ),
        "conversation_turns": RetentionPolicy(
            data_type="conversation_turns",
            retention_days=_env_days("RETENTION_CONVERSATIONS_DAYS", 90),
            description="Turn audit trail — CASCADE with parent conversation",
        ),
        "pending_actions": RetentionPolicy(
            data_type="pending_actions",
            retention_days=_env_days("RETENTION_CONVERSATIONS_DAYS", 90),
            description="Terminal booking proposals with customer details",
        ),
        "appointments": RetentionPolicy(
            data_type="appointments",
            retention_days=_env_days("RETENTION_APPOINTMENTS_DAYS", 365),
            description="Confirmed bookings with patient name/phone",
        ),
        "appointment_commits": RetentionPolicy(
            data_type="appointment_commits",
            retention_days=_env_days("RETENTION_APPOINTMENTS_DAYS", 365),
            description="Immutable booking evidence",
        ),
        # The call row itself is operational evidence (duration, outcome) and
        # is kept. Only the transcript is redacted -- it is the free text a
        # patient spoke, so it is the part that carries whatever they chose to
        # say about their health. DPDP purpose limitation: it exists to make a
        # booking, and it stops existing once it cannot serve that purpose.
        #
        # This deliberately does NOT cover calls.caller_phone. Phone retention
        # is per clinic instruction, which is not modelled yet -- see the note
        # in domain/compliance/consent.py. Redacting it here would be a policy
        # decision nobody has made.
        "call_transcripts": RetentionPolicy(
            data_type="call_transcripts",
            retention_days=_env_days("RETENTION_CALL_TRANSCRIPTS_DAYS", 90),
            description="Spoken call transcripts — redacted in place, call row retained",
        ),
        "notifications_delivered": RetentionPolicy(
            data_type="notifications_delivered",
            retention_days=_env_days("RETENTION_NOTIFICATIONS_DAYS", 30),
            description="Delivered notification records",
        ),
        "notifications_dead_letter": RetentionPolicy(
            data_type="notifications_dead_letter",
            retention_days=_env_days("RETENTION_NOTIFICATIONS_DEAD_LETTER_DAYS", 90),
            description="Dead-lettered notifications for debugging",
        ),
        "whatsapp_inbound_completed": RetentionPolicy(
            data_type="whatsapp_inbound_completed",
            retention_days=_env_days("RETENTION_WHATSAPP_INBOUND_DAYS", 30),
            description="Completed inbound WhatsApp events",
        ),
        "whatsapp_inbound_dead_letter": RetentionPolicy(
            data_type="whatsapp_inbound_dead_letter",
            retention_days=_env_days("RETENTION_WHATSAPP_INBOUND_DEAD_LETTER_DAYS", 30),
            description="Dead-lettered inbound events with message body",
        ),
    }
