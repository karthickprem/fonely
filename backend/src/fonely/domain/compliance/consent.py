"""The notice a patient hears before they say anything.

Under the DPDP Act the consent belongs to the patient, not to us and not to
the clinic. The clinic is the Data Fiduciary -- it decides why the data is
collected -- and Fonely is the Data Processor acting on its instructions.
That distinction decides the wording: the notice names the *clinic*, because
that is who the patient is dealing with and who they can complain to. It
never says "Fonely", which would name a company the patient has no
relationship with and cannot hold to anything.

Four things the notice has to do, and one it must not:

  * Name the clinic, so the fiduciary is identified.
  * Say the assistant is automated. A patient who thinks they are talking to
    the receptionist has not been told anything real.
  * State the purpose -- booking -- and nothing wider. Purpose limitation is
    the whole point; a notice that says "to serve you better" grants nothing
    because it means nothing.
  * Say the call is transcribed and the voice is not kept. This is the one
    patients actually react to.
  * Not run long. This plays before a patient has said a word, and they
    called to book an appointment. A thirty-second legal preamble gets hung
    up on, and a notice nobody hears to the end is not a notice. Two
    sentences is the budget. The grievance contact is deliberately kept out
    of the opening and answered on request instead -- see
    ``build_grievance_notice``.

The claim "your voice is not recorded" is load-bearing and is currently true
structurally rather than by policy: the `calls` table has a transcript column
and no audio column, so there is nowhere for audio to go. Anyone adding audio
storage has to come here and change this sentence first, or we are lying to
patients on every call. That is the intended tripwire.

TAMIL REVIEW REQUIRED. The Tamil below is written to be plain spoken Tamil
rather than the formal register a translator tends to produce, but it has not
been read by a native speaker yet and it is the first thing every patient
hears. It must be reviewed aloud before the dentist demo, not just read on a
screen -- TTS pronunciation is part of what is being checked.
"""

from __future__ import annotations

from typing import Any

# Bumped whenever the wording changes. Recorded against each call so that if
# the text is ever revised we can still say which version a given patient was
# actually read -- "we have a consent notice" is not evidence, "this patient
# heard v1 on this date" is.
NOTICE_VERSION = "1"

_DEFAULT_LOCALE = "en-IN"

_OPENING: dict[str, str] = {
    "ta-IN": (
        "வணக்கம், இது {clinic} இன் தானியங்கி முன்பதிவு உதவியாளர். "
        "உங்கள் உரையாடல் முன்பதிவுக்காக மட்டும் எழுத்தாக சேமிக்கப்படும், "
        "குரல் பதிவு செய்யப்படாது."
    ),
    "en-IN": (
        "Hello, this is {clinic}'s automated booking assistant. "
        "This conversation is saved as text for booking only, "
        "and your voice is not recorded."
    ),
}

_GRIEVANCE: dict[str, str] = {
    "ta-IN": ("உங்கள் தகவல் குறித்த எந்த கேள்விக்கும் {clinic} ஐ {contact} என்ற எண்ணில் தொடர்பு கொள்ளலாம்."),
    "en-IN": ("For any question about your information, you can contact {clinic} at {contact}."),
}


def _resolve(table: dict[str, str], locale: str) -> str:
    """Pick the wording for a locale, falling back to English.

    Falling back is correct here and would not be elsewhere. A patient who
    gets the notice in the wrong language has still been given a notice; a
    patient who gets silence because we had no Tamil string has not. Silence
    is the worse failure, so an unknown locale degrades rather than raises.
    """
    return table.get(locale) or table[_DEFAULT_LOCALE]


def build_opening_notice(clinic_name: str, locale: str = _DEFAULT_LOCALE) -> str:
    """The sentence spoken before the assistant asks anything.

    Raises if the clinic name is missing rather than speaking around the gap.
    A notice that fails to name the fiduciary is not a defective notice, it is
    not a notice at all -- and the degraded form is worse than the error,
    because "this is 's automated booking assistant" sounds like a broken
    robot in the patient's ear and still leaves them uninformed. Failing here
    surfaces at call setup, where someone can fix the clinic record.
    """
    name = clinic_name.strip()
    if not name:
        msg = "clinic_name is required: the DPDP notice must name the data fiduciary"
        raise ValueError(msg)
    return _resolve(_OPENING, locale).format(clinic=name)


def build_grievance_notice(
    clinic_name: str,
    contact: str,
    locale: str = _DEFAULT_LOCALE,
) -> str:
    """Where the patient goes with a question about their data.

    Kept out of the opening on purpose. The DPDP requirement is that the
    contact be reachable, not that it be recited to someone who did not ask;
    spending the opening's attention budget on a phone number nobody wrote
    down costs us the sentences that carry the actual disclosure.

    The contact is the *clinic's*, not ours. Fonely is the processor -- a
    patient sent to us would be sent to a company with no authority to answer
    them.
    """
    name = clinic_name.strip()
    reachable = contact.strip()
    if not name:
        msg = "clinic_name is required: the DPDP notice must name the data fiduciary"
        raise ValueError(msg)
    if not reachable:
        msg = "contact is required: a grievance route the patient cannot reach is not a route"
        raise ValueError(msg)
    return _resolve(_GRIEVANCE, locale).format(clinic=name, contact=reachable)


def notice_transcript_event(locale: str, spoken_text: str) -> dict[str, Any]:
    """A NON-AUTHORITATIVE transcript-context marker that the notice was read.

    This is human-readable context appended to `calls.transcript` JSONB — NOT
    the compliance record of authority. Under the finalized CEO #31 contract the
    authoritative evidence is the explicit `calls` columns
    (`dpdp_notice_completed_at` / `_version` / `_locale` / `_content_digest`)
    with a `num_nonnulls(...) IN (0, 4)` CHECK, written via
    `fonely.voice.evidence.DpdpEvidenceWriter`. A compliance query MUST read
    those columns, never a transcript substring — a transcript can be redacted
    for PII retention while the four columns are preserved, so treating the
    transcript as authority would make redaction look like missing consent.

    `spoken_text` is included here only as readable context; the tamper-evident
    proof of exactly-what-was-said is the content digest in the column, computed
    by `evidence.notice_content_digest` over the same spoken string.
    """
    return {
        "role": "system",
        "kind": "dpdp_notice",
        "notice_version": NOTICE_VERSION,
        "locale": locale,
        "text": spoken_text,
    }
