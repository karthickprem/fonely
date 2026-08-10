"""Audio seam probes — provider-free happy path and adversarial.

Tests the Exotel audio adapter against synthetic provider fixtures.
No live Exotel connection required.
"""

from __future__ import annotations

import base64
import json

import pytest

from fonely.api.channels.exotel_audio import (
    ExotelAudioAdapter,
    ExotelStreamError,
    parse_exotel_ws_message,
)
from fonely.domain.calls.media import (
    CANONICAL_INBOUND,
    CANONICAL_OUTBOUND,
    CONTRACT_VERSION,
    INBOUND_BYTES_PER_FRAME,
    OUTBOUND_BYTES_PER_FRAME,
    InboundAudioFrame,
    InboundDiscontinuity,
    OutboundAudioFrame,
    ProviderStreamEnded,
    SessionStarted,
)


def _make_adapter() -> ExotelAudioAdapter:
    return ExotelAudioAdapter(
        business_id=1,
        provider_environment="sandbox",
        provider_account_id="test_account",
    )


def _make_start_msg(
    codec: str = "audio/pcmu",
    rate: int = 8000,
) -> dict:
    return {
        "event": "start",
        "streamSid": "stream_" + "a" * 28,
        "callSid": "call_" + "b" * 27,
        "mediaFormat": {
            "encoding": codec,
            "sampleRate": rate,
        },
    }


def _make_ulaw_silence(num_samples: int) -> bytes:
    """Generate G.711 μ-law silence (0x7F = digital silence in μ-law)."""
    return b"\x7f" * num_samples


def _make_media_msg(
    payload_ulaw: bytes,
    chunk: int = 0,
    timestamp: int = 0,
) -> dict:
    return {
        "event": "media",
        "media": {
            "payload": base64.b64encode(payload_ulaw).decode(),
            "chunk": chunk,
            "timestamp": timestamp,
        },
    }


def _make_stop_msg(reason: str = "call_ended") -> dict:
    return {
        "event": "stop",
        "stop": {"reason": reason},
    }


# ============================================================================
# Happy path
# ============================================================================


class TestHappyPathSeam:
    def test_start_produces_session_started(self) -> None:
        adapter = _make_adapter()
        started = adapter.handle_start(_make_start_msg())
        assert isinstance(started, SessionStarted)
        assert started.identity.schema_version == CONTRACT_VERSION
        assert started.identity.business_id == 1
        assert started.identity.provider == "exotel"
        assert started.identity.provider_environment == "sandbox"
        assert started.input_format == CANONICAL_INBOUND
        assert started.output_format == CANONICAL_OUTBOUND
        assert started.started_monotonic_ns > 0

    def test_media_produces_640_byte_canonical_frame(self) -> None:
        """Accumulate enough provider audio to produce one 640-byte frame.

        ratecv 8k→16k doesn't produce exactly 2x output per chunk due to
        polyphase filter, so we send two chunks to guarantee one frame.
        """
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())

        ulaw_chunk = _make_ulaw_silence(160)
        adapter.handle_media(_make_media_msg(ulaw_chunk, chunk=0, timestamp=0))
        result = adapter.handle_media(_make_media_msg(ulaw_chunk, chunk=1, timestamp=20))

        assert result is not None
        assert isinstance(result, InboundAudioFrame)
        assert len(result.pcm_s16le_16khz_mono) == INBOUND_BYTES_PER_FRAME
        assert result.sequence == 0

    def test_outbound_encode_produces_ulaw(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())

        pcm_24k = b"\x00" * OUTBOUND_BYTES_PER_FRAME
        frame = OutboundAudioFrame(
            generation_id=1,
            sequence=0,
            media_timestamp_ms=0,
            pcm_s16le_24khz_mono=pcm_24k,
        )
        encoded = adapter.encode_outbound(frame)
        assert len(encoded) > 0
        decoded = base64.b64decode(encoded)
        assert len(decoded) > 0

    def test_stop_produces_ended(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())
        ended = adapter.handle_stop(_make_stop_msg())
        assert isinstance(ended, ProviderStreamEnded)
        assert ended.provider_code == "call_ended"

    def test_full_lifecycle(self) -> None:
        """Start → multiple media frames → stop."""
        adapter = _make_adapter()
        started = adapter.handle_start(_make_start_msg())
        assert isinstance(started, SessionStarted)

        frames = []
        for i in range(5):
            ulaw = _make_ulaw_silence(160)
            result = adapter.handle_media(
                _make_media_msg(ulaw, chunk=i, timestamp=i * 20)
            )
            if isinstance(result, InboundAudioFrame):
                frames.append(result)

        assert len(frames) > 0
        for i, f in enumerate(frames):
            assert f.sequence == i
            assert len(f.pcm_s16le_16khz_mono) == INBOUND_BYTES_PER_FRAME

        ended = adapter.handle_stop(_make_stop_msg())
        assert isinstance(ended, ProviderStreamEnded)

    def test_sequence_is_monotonic(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())

        seqs = []
        for i in range(10):
            ulaw = _make_ulaw_silence(160)
            result = adapter.handle_media(
                _make_media_msg(ulaw, chunk=i, timestamp=i * 20)
            )
            if isinstance(result, InboundAudioFrame):
                seqs.append(result.sequence)

        for i in range(1, len(seqs)):
            assert seqs[i] == seqs[i - 1] + 1

    def test_outbound_generation_filtering(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())

        old_frame = OutboundAudioFrame(
            generation_id=1, sequence=0, media_timestamp_ms=0,
            pcm_s16le_24khz_mono=b"\x00" * OUTBOUND_BYTES_PER_FRAME,
        )
        new_frame = OutboundAudioFrame(
            generation_id=2, sequence=0, media_timestamp_ms=0,
            pcm_s16le_24khz_mono=b"\x00" * OUTBOUND_BYTES_PER_FRAME,
        )

        assert adapter.should_send(old_frame)
        adapter.handle_clear(1)
        assert not adapter.should_send(old_frame)
        assert adapter.should_send(new_frame)


# ============================================================================
# Adversarial
# ============================================================================


class TestAdversarialSeam:
    def test_unsupported_codec_fails(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ExotelStreamError, match="unsupported codec"):
            adapter.handle_start(_make_start_msg(codec="audio/opus"))

    def test_wrong_sample_rate_fails(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ExotelStreamError, match="unsupported sample rate"):
            adapter.handle_start(_make_start_msg(rate=16000))

    def test_duplicate_start_fails(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())
        with pytest.raises(ExotelStreamError, match="duplicate start"):
            adapter.handle_start(_make_start_msg())

    def test_media_before_start_fails(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ExotelStreamError, match="media before start"):
            adapter.handle_media(_make_media_msg(b"\x7f" * 160))

    def test_missing_stream_sid_fails(self) -> None:
        adapter = _make_adapter()
        msg = _make_start_msg()
        msg["streamSid"] = ""
        with pytest.raises(ExotelStreamError, match="missing streamSid"):
            adapter.handle_start(msg)

    def test_invalid_base64_payload_fails(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())
        msg = {
            "event": "media",
            "media": {"payload": "not-valid-base64!!!"},
        }
        with pytest.raises(ExotelStreamError, match="invalid base64"):
            adapter.handle_media(msg)

    def test_timestamp_regression_produces_discontinuity(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())

        adapter.handle_media(
            _make_media_msg(_make_ulaw_silence(160), timestamp=100)
        )
        result = adapter.handle_media(
            _make_media_msg(_make_ulaw_silence(160), timestamp=50)
        )
        assert isinstance(result, InboundDiscontinuity)
        assert result.reason == "provider_reset"

    def test_outbound_wrong_size_fails(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())
        frame = OutboundAudioFrame(
            generation_id=1, sequence=0, media_timestamp_ms=0,
            pcm_s16le_24khz_mono=b"\x00" * 100,
        )
        with pytest.raises(ExotelStreamError, match="wrong size"):
            adapter.encode_outbound(frame)

    def test_barge_in_clears_old_generation(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())

        old = OutboundAudioFrame(
            generation_id=1, sequence=0, media_timestamp_ms=0,
            pcm_s16le_24khz_mono=b"\x00" * OUTBOUND_BYTES_PER_FRAME,
        )
        assert adapter.should_send(old)

        adapter.handle_clear(1)

        assert not adapter.should_send(old)
        late = OutboundAudioFrame(
            generation_id=1, sequence=1, media_timestamp_ms=20,
            pcm_s16le_24khz_mono=b"\x00" * OUTBOUND_BYTES_PER_FRAME,
        )
        assert not adapter.should_send(late)

    def test_disconnect_during_active_stream(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())
        adapter.handle_media(
            _make_media_msg(_make_ulaw_silence(160), timestamp=0)
        )
        ended = adapter.handle_stop(None)
        assert isinstance(ended, ProviderStreamEnded)
        assert ended.reason == "normal_disconnect"

    def test_error_stop_reason(self) -> None:
        adapter = _make_adapter()
        adapter.handle_start(_make_start_msg())
        ended = adapter.handle_stop(
            {"event": "stop", "stop": {"reason": "network_error_timeout"}}
        )
        assert ended.reason == "network_error"

    def test_oversized_ws_message_rejected(self) -> None:
        with pytest.raises(ExotelStreamError, match="too large"):
            parse_exotel_ws_message(b"x" * 70_000)

    def test_non_json_ws_message_rejected(self) -> None:
        with pytest.raises(ExotelStreamError, match="invalid JSON"):
            parse_exotel_ws_message(b"not json")

    def test_missing_event_type_rejected(self) -> None:
        with pytest.raises(ExotelStreamError, match="missing event type"):
            parse_exotel_ws_message(json.dumps({"data": "stuff"}).encode())

    def test_non_object_json_rejected(self) -> None:
        with pytest.raises(ExotelStreamError, match="expected JSON object"):
            parse_exotel_ws_message(json.dumps([1, 2, 3]).encode())
