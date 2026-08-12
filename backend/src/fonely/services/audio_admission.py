"""Tenant admission for an inbound audio stream.

The problem this exists to solve
--------------------------------
The audio-stream WebSocket authenticates a shared secret. That proves the
connection came from our telephony provider. It does not say *which clinic the
patient dialed*, and the handler had no business_id at all — so there was
nowhere safe to mount a voice pipeline that books appointments.

The tempting fix is to read the tenant out of the stream's opening frame,
which carries a CallSid and, on some providers, the dialed number. That would
hand anyone holding the shared secret a session against any clinic in the
system: one leaked applet URL and a caller books, cancels, and reads back
appointments for a clinic they have nothing to do with. Provider-supplied
identity is a claim, never a fact.

What we do instead
------------------
Identity is established by *our own prior observation*. The call-status
webhook fires on ringing before any media flows; that handler resolves the
dialed number to a tenant through business_channel_identities and writes a
calls row stamped with the provider's call id. Admission then looks that row
up. The CallSid arriving on the socket is used only as a lookup key — it
selects a row, it never supplies a value. If no row exists, the call was never
observed ringing and the stream is refused.

The consequences are worth stating plainly:

  * A forged or guessed CallSid resolves to nothing and is refused. Even a
    correctly guessed one only ever yields the tenant *we* recorded at ringing.
  * A call that has already completed will not admit a second stream, so a
    replayed opening frame cannot reopen a finished conversation.
  * If the ringing webhook is misconfigured, every call is refused rather than
    silently defaulting to some tenant. That is the correct failure: a clinic
    whose calls do not connect gets fixed, a clinic quietly booking another
    clinic's patients does not get noticed.

Refusal carries a reason, and "refused" is a distinct outcome from "admitted
with nothing configured". Absence must not read as success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("fonely.services.audio_admission")


class AdmissionRefusal(StrEnum):
    """Why a stream was refused. Distinct values, never collapsed to a bool."""

    NO_CALL_IDENTIFIER = "no_call_identifier"
    """The stream never presented a call id, so it cannot be correlated."""

    UNOBSERVED_CALL = "unobserved_call"
    """No ringing webhook was recorded for this call id. The usual cause is a
    forged identifier; the other is a misconfigured status callback, and both
    must refuse."""

    CALL_ALREADY_ENDED = "call_already_ended"
    """The call has an ended_at. A finished conversation is not reopenable."""


@dataclass(frozen=True)
class AudioSession:
    """Trusted context for one admitted audio stream.

    Every field here was resolved server-side from our own records. Nothing on
    it came from the socket except provider_call_sid, which was used as a
    lookup key and then re-read from the row it selected.

    This is the interface the voice runtime consumes. It deliberately carries
    clinic_name and timezone as well as ids: the DPDP notice must name the
    clinic, and the dialogue must ground dates in the clinic's timezone, and
    both should come from the same trusted resolution that bound the tenant
    rather than from a second lookup that could disagree with it.
    """

    business_id: int
    call_id: int
    caller_phone: str | None
    clinic_name: str
    timezone: str
    provider: str
    provider_call_sid: str


@dataclass(frozen=True)
class AdmissionResult:
    """Either an admitted session or a reason it was refused."""

    session: AudioSession | None
    refusal: AdmissionRefusal | None

    @property
    def admitted(self) -> bool:
        return self.session is not None


async def admit_audio_stream(
    db_session: AsyncSession,
    *,
    provider: str,
    provider_call_sid: str,
) -> AdmissionResult:
    """Resolve trusted tenant context for an inbound audio stream.

    Returns an AdmissionResult whose session is None unless the call was
    observed ringing by our own webhook and has not yet ended.
    """
    if not provider_call_sid:
        return AdmissionResult(session=None, refusal=AdmissionRefusal.NO_CALL_IDENTIFIER)

    result = await db_session.execute(
        text(
            "SELECT c.id, c.business_id, c.caller_phone, c.ended_at, "
            "       b.name, b.timezone "
            "FROM calls c "
            "JOIN businesses b ON b.id = c.business_id "
            "WHERE c.call_provider = :provider "
            "  AND c.provider_call_sid = :sid"
        ),
        {"provider": provider, "sid": provider_call_sid},
    )
    row = result.one_or_none()

    if row is None:
        # Deliberately not logging the presented sid at warning level with the
        # rest of the context: this fires on forged input, and an attacker
        # should not be able to fill shared logs with chosen strings.
        logger.warning("audio_admission_unobserved_call", extra={"provider": provider})
        return AdmissionResult(session=None, refusal=AdmissionRefusal.UNOBSERVED_CALL)

    call_id, business_id, caller_phone, ended_at, clinic_name, timezone = row

    if ended_at is not None:
        logger.warning(
            "audio_admission_call_already_ended",
            extra={"provider": provider, "call_id": call_id},
        )
        return AdmissionResult(session=None, refusal=AdmissionRefusal.CALL_ALREADY_ENDED)

    logger.info(
        "audio_admission_admitted",
        extra={
            "provider": provider,
            "call_id": call_id,
            "business_id": business_id,
        },
    )
    return AdmissionResult(
        session=AudioSession(
            business_id=int(business_id),
            call_id=int(call_id),
            caller_phone=caller_phone,
            clinic_name=clinic_name,
            timezone=timezone,
            provider=provider,
            provider_call_sid=provider_call_sid,
        ),
        refusal=None,
    )
