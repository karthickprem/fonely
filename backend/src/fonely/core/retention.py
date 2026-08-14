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


def callback_ttl_days() -> int:
    """Days a voice give-up callback lives before it self-expires.

    Single source for BOTH expiry paths: the callback's ``expires_at`` at
    creation and the ``callbacks`` retention sweep window read the SAME value,
    so a callback dies by whichever fires first and neither can outlive this
    horizon. Own env key (not RETENTION_CONVERSATIONS_DAYS / OFFER_TTL) so ops
    can tune callback PII lifetime independently.
    """
    return _env_days("CALLBACK_TTL_DAYS", 90)


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
        # Callbacks are pending_actions too, but the existing pending_actions
        # sweep only deletes rows that COMMITTED an entity (committed_entity_id
        # IS NOT NULL) — correct for bookings, but a callback never commits one,
        # so it would be structurally immortal under that policy. It carries
        # caller PII + booking intent, so it needs its own bounded horizon and a
        # sweep branch that does not require a committed entity. Own env key so
        # ops can tune callback lifetime independently; the same value governs
        # the callback's expires_at (belt-and-suspenders: a callback dies by its
        # own expiry OR this sweep, whichever fires first).
        "callbacks": RetentionPolicy(
            data_type="callbacks",
            retention_days=callback_ttl_days(),
            description="Voice give-up callbacks with caller phone + booking intent",
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
