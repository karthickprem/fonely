"""Exotel protocol simulator — local fake client for transport testing.

Speaks the documented AgentStream VoiceBot wire protocol:
- JSON control messages (connected, start, media, dtmf, mark, stop)
- Raw PCM s16le audio, base64 in JSON, chunks divisible by 320
- Bidirectional: sends inbound audio, receives outbound audio

Used for acceptance testing. Green probes prove admission logic is
correct; they do NOT prove Exotel behaves as documented.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class SimulatorConfig:
    call_sid: str = ""
    stream_sid: str = ""
    account_sid: str = "AC_simulator"
    from_number: str = "+919000000001"
    to_number: str = "08012345678"
    encoding: str = "audio/x-raw"
    sample_rate: int = 16000
    bit_rate: int = 16
    custom_parameters: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.call_sid:
            self.call_sid = "CA" + uuid4().hex[:30]
        if not self.stream_sid:
            self.stream_sid = "MZ" + uuid4().hex[:30]


class ExotelSimulator:
    """Fake Exotel client that sends protocol-correct messages.

    Usage:
        sim = ExotelSimulator(config)
        messages = sim.connected() + sim.start() + sim.media_frames(5)
        messages.append(sim.stop())
    """

    def __init__(self, config: SimulatorConfig | None = None) -> None:
        self.config = config or SimulatorConfig()
        self._seq = 0
        self._chunk = -1
        self._timestamp_ms = 0
        self.received_outbound: list[dict[str, Any]] = []
        self.received_clears: list[dict[str, Any]] = []
        self.received_marks: list[dict[str, Any]] = []

    def _next_seq(self) -> str:
        self._seq += 1
        return str(self._seq)

    def connected_msg(self) -> str:
        return json.dumps({"event": "connected"})

    def start_msg(self) -> str:
        return json.dumps({
            "event": "start",
            "sequence_number": self._next_seq(),
            "stream_sid": self.config.stream_sid,
            "start": {
                "stream_sid": self.config.stream_sid,
                "call_sid": self.config.call_sid,
                "account_sid": self.config.account_sid,
                "from": self.config.from_number,
                "to": self.config.to_number,
                "custom_parameters": self.config.custom_parameters,
                "media_format": {
                    "encoding": self.config.encoding,
                    "sample_rate": str(self.config.sample_rate),
                    "bit_rate": str(self.config.bit_rate),
                },
            },
        })

    def media_msg(self, pcm_bytes: bytes | None = None) -> str:
        """Generate a media message with PCM audio.

        If pcm_bytes is None, generates silence. Chunk size must be
        divisible by 320 per Exotel docs.
        """
        if pcm_bytes is None:
            frame_bytes = self.config.sample_rate * 2 * 20 // 1000
            pcm_bytes = b"\x00" * frame_bytes

        self._chunk += 1
        chunk_duration_ms = len(pcm_bytes) * 1000 // (self.config.sample_rate * 2)
        self._timestamp_ms += chunk_duration_ms

        return json.dumps({
            "event": "media",
            "sequence_number": self._next_seq(),
            "stream_sid": self.config.stream_sid,
            "media": {
                "chunk": str(self._chunk),
                "timestamp": str(self._timestamp_ms),
                "payload": base64.b64encode(pcm_bytes).decode(),
            },
        })

    def media_frames(self, count: int) -> list[str]:
        return [self.media_msg() for _ in range(count)]

    def stop_msg(self, reason: str = "callended") -> str:
        return json.dumps({
            "event": "stop",
            "sequence_number": self._next_seq(),
            "stream_sid": self.config.stream_sid,
            "stop": {
                "call_sid": self.config.call_sid,
                "account_sid": self.config.account_sid,
                "reason": reason,
            },
        })

    def dtmf_msg(self, digit: str, duration: int = 100) -> str:
        return json.dumps({
            "event": "dtmf",
            "sequence_number": self._next_seq(),
            "stream_sid": self.config.stream_sid,
            "dtmf": {
                "digit": digit,
                "duration": str(duration),
            },
        })

    def status_callback(
        self,
        event_type: str = "terminal",
        status: str = "completed",
        duration: str = "60",
        direction: str = "inbound",
    ) -> dict[str, str]:
        """Generate a status callback payload (HTTP POST, not WS)."""
        return {
            "CallSid": self.config.call_sid,
            "EventType": event_type,
            "Status": status,
            "From": self.config.from_number,
            "To": self.config.to_number,
            "Duration": duration,
            "Direction": direction,
        }

    # --- Unhappy path generators ---

    def media_msg_gap(self, skip_to_chunk: int) -> str:
        """Generate media with a sequence gap."""
        self._chunk = skip_to_chunk
        return self.media_msg()

    def media_msg_timestamp_regression(self, timestamp_ms: int) -> str:
        """Generate media with a timestamp regression but sequential chunk."""
        pcm_bytes = b"\x00" * (self.config.sample_rate * 2 * 20 // 1000)
        self._chunk += 1
        self._timestamp_ms = timestamp_ms
        return json.dumps({
            "event": "media",
            "sequence_number": self._next_seq(),
            "stream_sid": self.config.stream_sid,
            "media": {
                "chunk": str(self._chunk),
                "timestamp": str(timestamp_ms),
                "payload": base64.b64encode(pcm_bytes).decode(),
            },
        })

    def status_callback_wrong_business(self) -> dict[str, str]:
        """Callback claiming a number that maps to a different business."""
        return {
            "CallSid": self.config.call_sid,
            "EventType": "terminal",
            "Status": "completed",
            "From": "+919999999999",
            "To": "09999999999",
            "Duration": "30",
        }

    def status_callback_before_start(self) -> dict[str, str]:
        """Status callback that arrives before media/start — pending case."""
        return self.status_callback()
