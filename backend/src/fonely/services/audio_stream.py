"""The opening handshake of a media stream, parsed once and handed on.

Why this is a module and not four lines in the telephony adapter
----------------------------------------------------------------
A media-stream socket opens with control frames before any audio: a
``connected`` event, then exactly one ``start`` event carrying the stream
identifier and the audio format the provider is about to send. The adapter
already had to read those frames, because when the console cannot template the
call id into the URL they are the only place it appears.

It read them and threw them away. That is fine right up until something
downstream needs them — and everything downstream does. A serializer cannot
send a frame back without the stream id, and a transport cannot decode audio
without knowing the rate it arrives at. Control frames are not replayed by the
provider, so whatever the adapter consumed is gone for good. The result was a
seam that authenticated a call correctly and then made it impossible to answer.

So the frames are parsed into a typed value and handed forward, including the
raw text of each one, so a serializer that wants to parse the start event
itself still can. Exactly one component reads each frame off the socket, and
nothing after that point has to guess what was in it.

What is trusted here and what is not
------------------------------------
Nothing on this path establishes identity. The stream id, the declared format
and the call id in the start event are all provider claims arriving on a socket,
and they are treated as claims: the call id is a lookup key handed to admission,
which resolves the tenant from our own records and ignores everything else the
frame said. See services/audio_admission.py.

What this module does enforce is that the claims are well-formed and that we
can actually honour them. A declared sample rate we do not support is refused
rather than accepted and silently resampled into noise, and audio arriving
before the format has been declared is refused rather than decoded on a guess.
Both fail closed, with a distinct reason, because "we refused this stream" and
"this stream arrived and nothing happened" must never look the same in a log.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger("fonely.services.audio_stream")

# A control frame is a few hundred bytes. The cap exists so a socket holding a
# valid secret cannot make us buffer arbitrary memory before it has identified
# itself; it is not a guess at the largest legitimate frame.
MAX_CONTROL_FRAME_BYTES = 64 * 1024

# Rates we can actually run a pipeline at. Telephony is 8 kHz; the wideband
# rates are here because provider media-stream applets can be configured for
# them and a hardcoded 8000 would then decode every call into noise.
SUPPORTED_SAMPLE_RATES = (8000, 16000, 24000)

# Declared encodings, normalised to the two we handle. Providers spell these
# inconsistently across consoles and docs, so the spellings are enumerated
# rather than pattern-matched — an unrecognised one must refuse loudly, not
# fall through to a default that happens to be right most of the time.
_ENCODING_ALIASES = {
    "l16": "l16",
    "pcm": "l16",
    "pcm_s16le": "l16",
    "audio/x-l16": "l16",
    "audio/l16": "l16",
    "mulaw": "mulaw",
    "ulaw": "mulaw",
    "pcmu": "mulaw",
    "audio/x-mulaw": "mulaw",
}

# Only mono. A stereo telephony stream would mean two legs interleaved, and
# feeding that to STT transcribes both speakers as one.
_SUPPORTED_CHANNELS = 1

_EVENT_KEYS = ("event", "type")
_STREAM_SID_KEYS = ("stream_sid", "streamSid", "streamsid")
_CALL_SID_KEYS = ("CallSid", "call_sid", "callsid")
_MEDIA_FORMAT_KEYS = ("media_format", "mediaFormat")


class StreamStartRefusal(StrEnum):
    """Why an opening handshake was refused. Never collapsed to a bool."""

    NO_START_EVENT = "no_start_event"
    """The frame budget ran out before a start event arrived."""

    MEDIA_BEFORE_START = "media_before_start"
    """Audio arrived before the format was declared. Decoding it would mean
    guessing the rate, and a wrong guess is not silence — it is a plausible
    stream of noise that STT will happily transcribe as words."""

    DUPLICATE_START = "duplicate_start"
    """A second start event on one socket. Either the provider is confused or
    someone is replaying frames; both are refusals."""

    MALFORMED_START = "malformed_start"
    """Not JSON, not an object, or missing the stream id or media format."""

    OVERSIZED_FRAME = "oversized_frame"
    """A control frame past MAX_CONTROL_FRAME_BYTES."""

    UNSUPPORTED_ENCODING = "unsupported_encoding"
    UNSUPPORTED_SAMPLE_RATE = "unsupported_sample_rate"
    UNSUPPORTED_CHANNELS = "unsupported_channels"


@dataclass(frozen=True)
class MediaStreamStart:
    """The provider's declared shape for one media stream.

    Provider-neutral field names deliberately: the voice runtime is built
    against this type, not against Exotel, so a second carrier is a new parser
    here rather than a fork of the pipeline. ``stream_sid`` is whatever the
    provider calls its stream handle — outbound frames have to quote it back.

    None of this is identity. The tenant comes from AudioSession.
    """

    stream_sid: str
    provider_call_sid: str
    encoding: str
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class AudioStreamHandoff:
    """Everything read off the socket before the runtime took over.

    ``raw_frames`` is every frame this module consumed, in arrival order,
    exactly as received. A serializer that would rather parse the provider's
    start event itself can do so; nothing was lost by us reading it first.
    """

    start: MediaStreamStart
    raw_frames: tuple[str, ...]


@dataclass(frozen=True)
class StreamStartResult:
    """Either a parsed handoff or the reason the stream was refused."""

    handoff: AudioStreamHandoff | None
    refusal: StreamStartRefusal | None

    @property
    def ok(self) -> bool:
        return self.handoff is not None


def _first_str(container: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = container.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _coerce_int(value: Any) -> int | None:
    """Accept 8000 and "8000" alike; refuse 8000.5 and "eight thousand".

    Providers send these as strings often enough that requiring int would
    refuse valid calls, and bool is excluded because bool is an int in Python
    and ``channels: true`` must not read as mono.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def frame_event(raw: str) -> str:
    """The event name of a control frame, lowercased, or "" if unparseable."""
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return _first_str(payload, _EVENT_KEYS).lower()


def call_sid_from_frame(raw: str) -> str:
    """Pull a provider call id out of one control frame, or return "".

    Checked at the top level and under ``start``, in every spelling we have
    seen, because discovering the shape during a live call is not a plan.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""

    containers: list[dict[str, Any]] = [payload]
    nested = payload.get("start")
    if isinstance(nested, dict):
        containers.append(nested)

    for container in containers:
        found = _first_str(container, _CALL_SID_KEYS)
        if found:
            return found
    return ""


def parse_start_frame(raw: str) -> MediaStreamStart | StreamStartRefusal:
    """Validate one start event into a MediaStreamStart, or say why not.

    Returns the refusal rather than raising: the caller has a socket to close
    with a specific reason, and an exception would flatten eight distinct
    causes into one except block.
    """
    if len(raw.encode("utf-8", errors="ignore")) > MAX_CONTROL_FRAME_BYTES:
        return StreamStartRefusal.OVERSIZED_FRAME

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return StreamStartRefusal.MALFORMED_START
    if not isinstance(payload, dict):
        return StreamStartRefusal.MALFORMED_START

    body = payload.get("start")
    body = body if isinstance(body, dict) else payload

    # The stream id lives at the top level on some providers and inside start
    # on others; take whichever is present.
    stream_sid = _first_str(body, _STREAM_SID_KEYS) or _first_str(payload, _STREAM_SID_KEYS)
    if not stream_sid:
        return StreamStartRefusal.MALFORMED_START

    media_format: dict[str, Any] | None = None
    for source in (body, payload):
        for key in _MEDIA_FORMAT_KEYS:
            candidate = source.get(key)
            if isinstance(candidate, dict):
                media_format = candidate
                break
        if media_format is not None:
            break
    if media_format is None:
        return StreamStartRefusal.MALFORMED_START

    declared_encoding = _first_str(media_format, ("encoding",)).strip().lower()
    encoding = _ENCODING_ALIASES.get(declared_encoding)
    if encoding is None:
        return StreamStartRefusal.UNSUPPORTED_ENCODING

    sample_rate = _coerce_int(media_format.get("sample_rate", media_format.get("sampleRate")))
    if sample_rate not in SUPPORTED_SAMPLE_RATES:
        return StreamStartRefusal.UNSUPPORTED_SAMPLE_RATE

    # Absent channels means mono on every provider we have read. Present and
    # not 1 is refused rather than assumed.
    raw_channels = media_format.get("channels")
    channels = _SUPPORTED_CHANNELS if raw_channels is None else _coerce_int(raw_channels)
    if channels != _SUPPORTED_CHANNELS:
        return StreamStartRefusal.UNSUPPORTED_CHANNELS

    return MediaStreamStart(
        stream_sid=stream_sid,
        provider_call_sid=call_sid_from_frame(raw),
        encoding=encoding,
        sample_rate=sample_rate,
        channels=channels,
    )
