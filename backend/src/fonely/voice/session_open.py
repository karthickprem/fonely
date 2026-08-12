"""Session-open sequence: the DPDP notice, spoken before any capture.

Under the DPDP Act the patient must be told, before they say a word, who they
are dealing with, that the assistant is automated, that the call is transcribed
for booking only, and that their voice is not kept. domain/compliance/consent.py
owns the wording (platform-owned, names the clinic not Fonely, two-sentence
budget). This module sequences it: notice FIRST, then the greeting, and produces
the evidence event to persist on the call record.

The notice text and version are recorded per call — "we have a notice" is not
evidence; "this patient heard v1 on this date" is. The evidence goes into the
existing calls.transcript JSONB via notice_transcript_event (no migration).

This is transport-neutral: the browser demo and the real telephony path both
call open_session() to get (spoken_lines, evidence_event) and are responsible
for speaking the lines before opening the mic and persisting the event.
"""

from __future__ import annotations

from dataclasses import dataclass

from fonely.domain.compliance.consent import (
    NOTICE_VERSION,
    build_opening_notice,
    notice_transcript_event,
)


@dataclass(frozen=True)
class SessionOpening:
    """What to speak at session start and the evidence to persist.

    spoken_lines[0] is the DPDP notice — it MUST be spoken before any
    conversational capture begins. notice_event is written to the call record
    as proof this patient heard this exact notice version.
    """

    notice_text: str
    greeting_text: str
    notice_event: dict[str, object]
    notice_version: str

    @property
    def spoken_lines(self) -> tuple[str, str]:
        """Notice first, greeting second — the exact order to synthesize."""
        return (self.notice_text, self.greeting_text)


def open_session(
    *,
    clinic_name: str,
    greeting_text: str,
    locale: str = "ta-IN",
) -> SessionOpening:
    """Build the session-open sequence for a call.

    The notice is spoken before the greeting and before the mic opens. Raises
    (via build_opening_notice) if the clinic name is missing — a notice that
    cannot name the data fiduciary is not a notice, and speaking around the gap
    would be worse than failing here.
    """
    notice_text = build_opening_notice(clinic_name, locale)
    notice_event = notice_transcript_event(locale, notice_text)
    return SessionOpening(
        notice_text=notice_text,
        greeting_text=greeting_text,
        notice_event=notice_event,
        notice_version=NOTICE_VERSION,
    )
