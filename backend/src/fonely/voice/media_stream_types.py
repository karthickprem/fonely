"""Structural types for the admitted-media handoff the voice runtime consumes.

These are the seam between the admission lane (which owns parsing, tenant
resolution, and the DB `calls` row) and this voice runtime. They are declared
here as runtime ``Protocol``s so the runtime depends on the SHAPE of the
handoff, not on the admission module's import path — the real dataclasses land
later in ``fonely.services.audio_stream`` / ``fonely.services.audio_admission``
and the swap is a one-line import change at the call site.

Field names are the frozen contract from the delivery controller and must not
drift. Three rules the runtime lives by, encoded in these docstrings so they
cannot be forgotten:

  * ``encoding`` is already normalised to exactly ``"l16"`` or ``"mulaw"``. The
    runtime never sees a provider's raw spelling and must never re-alias it.
  * ``sample_rate`` is validated upstream (an unsupported rate is refused before
    the runtime is called). The runtime hardcodes no rate — the serializer's
    wire rate comes straight from ``MediaStreamStart.sample_rate``.
  * ``raw_frames`` is a READ-ONLY inbound audit / re-parse surface: every frame
    the admission lane consumed before handing over the socket, verbatim and in
    order. It is never re-serialized outbound. The only start-event value that
    belongs in an outbound frame is ``stream_sid`` (from the typed field).

Nothing in ``MediaStreamStart`` is identity. Tenant/business/clinic/caller come
ONLY from ``AudioSession``, which is constructed ONLY by the admission lane —
never by this runtime, and never in ``src`` (test helpers build fakes).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MediaStreamStart(Protocol):
    """The validated start of one provider media stream (provider-neutral)."""

    @property
    def stream_sid(self) -> str:
        """Provider's stream handle. Outbound frames must quote this back."""
        ...

    @property
    def provider_call_sid(self) -> str:
        """Lookup key handed to admission — NOT identity."""
        ...

    @property
    def encoding(self) -> str:
        """Normalised codec, exactly one of ``"l16"`` | ``"mulaw"``."""
        ...

    @property
    def sample_rate(self) -> int:
        """Validated wire rate, one of 8000 | 16000 | 24000. Never defaulted."""
        ...

    @property
    def channels(self) -> int:
        """Always 1."""
        ...


@runtime_checkable
class AudioStreamHandoff(Protocol):
    """What the admission lane hands the runtime: the validated start plus every
    frame it read off the socket before handover, verbatim."""

    @property
    def start(self) -> MediaStreamStart: ...

    @property
    def raw_frames(self) -> tuple[str, ...]:
        """Frames consumed before the runtime got the socket, in arrival order.
        READ-ONLY: for re-parse/audit only, never re-serialized outbound."""
        ...


@runtime_checkable
class AudioSession(Protocol):
    """The admitted, DB-backed session identity. The sole source of tenant and
    caller identity for the call — constructed only by the admission lane."""

    @property
    def business_id(self) -> int: ...

    @property
    def call_id(self) -> int: ...

    @property
    def caller_phone(self) -> str: ...

    @property
    def clinic_name(self) -> str: ...

    @property
    def timezone(self) -> str:
        """IANA zone, e.g. ``"Asia/Kolkata"``."""
        ...

    @property
    def provider(self) -> str: ...

    @property
    def provider_call_sid(self) -> str: ...
