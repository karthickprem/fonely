"""Exotel stream entrypoint tests — start validation, rate derivation, drift.

Conditions tested:
- C1: Pinned Pipecat 1.7 ExotelFrameSerializer exists and has exotel_sample_rate
- C2: Sample rate probe — 1kHz PCM fixture survives serializer at 8k/16k/24k
- C6: Start event validation — ordering, malformed metadata, fail closed
- C7: No custom exotel_audio codec path production-reachable

Simulator-level (no live Exotel).
"""

from __future__ import annotations

import base64
import json
import math
import struct

import pytest

from fonely.api.channels.exotel_stream import (
    ExotelStartValidationError,
    check_rate_drift,
    parse_ws_start_message,
    validate_start_event,
)

# ============================================================================
# Condition 1: Pinned Pipecat serializer exists
# ============================================================================


class TestPipecatSerializerPinned:
    def test_exotel_serializer_importable(self) -> None:
        from pipecat.serializers.exotel import ExotelFrameSerializer

        assert ExotelFrameSerializer is not None

    def test_serializer_has_exotel_sample_rate_param(self) -> None:
        from pipecat.serializers.exotel import ExotelFrameSerializer

        params = ExotelFrameSerializer.InputParams(exotel_sample_rate=16000)
        assert params.exotel_sample_rate == 16000

    def test_serializer_default_rate_is_8000(self) -> None:
        from pipecat.serializers.exotel import ExotelFrameSerializer

        params = ExotelFrameSerializer.InputParams()
        assert params.exotel_sample_rate == 8000


# ============================================================================
# Condition 2: 1kHz golden probe — duration survives serializer
# ============================================================================


def _generate_1khz_pcm(sample_rate: int, duration_ms: int) -> bytes:
    """Generate a 1kHz sine wave as s16le PCM."""
    num_samples = sample_rate * duration_ms // 1000
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        sample = int(32000 * math.sin(2 * math.pi * 1000 * t))
        samples.append(struct.pack("<h", max(-32768, min(32767, sample))))
    return b"".join(samples)


def _measure_dominant_frequency(pcm: bytes, sample_rate: int) -> float:
    """Measure the dominant frequency in PCM audio using zero-crossing."""
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    crossings = 0
    for i in range(1, len(samples)):
        if (samples[i - 1] >= 0) != (samples[i] >= 0):
            crossings += 1
    duration_s = len(samples) / sample_rate
    return crossings / (2 * duration_s)


class TestSampleRateGoldenProbe:
    """Prove a known 1kHz PCM fixture's duration and frequency survive
    the ExotelFrameSerializer at 8k, 16k, and 24k.

    This is the probe that detects double-speed conversion.
    """

    @pytest.mark.parametrize("rate", [8000, 16000, 24000])
    async def test_1khz_survives_serializer_roundtrip(self, rate: int) -> None:
        """1kHz tone at declared rate → serialize → deserialize → frequency preserved.

        Stream resamplers buffer the first chunk, so we send 3 chunks
        and verify the output from later chunks.
        """
        from pipecat.frames.frames import StartFrame
        from pipecat.serializers.exotel import ExotelFrameSerializer

        serializer = ExotelFrameSerializer(
            stream_sid="MZ_test",
            params=ExotelFrameSerializer.InputParams(
                exotel_sample_rate=rate,
                sample_rate=16000,
            ),
        )
        await serializer.setup(StartFrame(audio_in_sample_rate=16000))

        collected_pcm = bytearray()
        for _chunk_idx in range(3):
            pcm_chunk = _generate_1khz_pcm(rate, 100)
            payload = base64.b64encode(pcm_chunk).decode()
            exotel_msg = json.dumps(
                {
                    "event": "media",
                    "media": {"payload": payload},
                }
            )
            frame = await serializer.deserialize(exotel_msg)
            if frame is not None:
                assert frame.sample_rate == 16000
                collected_pcm.extend(frame.audio)

        assert len(collected_pcm) >= 640, f"expected output audio, got {len(collected_pcm)} bytes"

        freq = _measure_dominant_frequency(bytes(collected_pcm), 16000)
        assert 800 < freq < 1200, f"expected ~1000Hz, got {freq:.0f}Hz at input rate {rate}"

    @pytest.mark.parametrize("rate", [8000, 16000, 24000])
    async def test_duration_preserved(self, rate: int) -> None:
        """1s of audio at declared rate → duration ratio close to 1.0.

        Derived tolerance: soxr stream resampler buffers up to one
        filter-length of samples (~100ms at 8kHz). Over 1s of input,
        that's at most 10% loss. A rate mismatch (e.g. declaring 8k
        but feeding 16k) produces a 2x error = 100% overshoot.

        Tolerance: 0.85-1.15 (15% margin, catches anything >30% error).
        Smallest detectable rate mismatch: 2x = 100% >> 15%.
        """
        from pipecat.frames.frames import StartFrame
        from pipecat.serializers.exotel import ExotelFrameSerializer

        serializer = ExotelFrameSerializer(
            stream_sid="MZ_test",
            params=ExotelFrameSerializer.InputParams(
                exotel_sample_rate=rate,
                sample_rate=16000,
            ),
        )
        await serializer.setup(StartFrame(audio_in_sample_rate=16000))

        num_chunks = 10
        total_input_ms = num_chunks * 100
        collected_pcm = bytearray()
        for _ in range(num_chunks):
            pcm_chunk = _generate_1khz_pcm(rate, 100)
            payload = base64.b64encode(pcm_chunk).decode()
            exotel_msg = json.dumps(
                {
                    "event": "media",
                    "media": {"payload": payload},
                }
            )
            frame = await serializer.deserialize(exotel_msg)
            if frame is not None:
                collected_pcm.extend(frame.audio)

        output_samples = len(collected_pcm) // 2
        output_duration_ms = output_samples * 1000 / 16000
        ratio = output_duration_ms / total_input_ms
        assert 0.85 < ratio < 1.15, (
            f"duration ratio {ratio:.2f} at input rate {rate}: "
            f"expected ~{total_input_ms}ms, got {output_duration_ms:.1f}ms"
        )

    async def test_wrong_rate_detected_by_duration_probe(self) -> None:
        """NEGATIVE CONTROL: declare 8kHz, feed 16kHz audio → duration
        probe detects the mismatch.

        If this test ever passes (i.e. the assertion does NOT fire),
        the positive duration probes above are not detecting the
        double-speed bug they exist to catch.
        """
        from pipecat.frames.frames import StartFrame
        from pipecat.serializers.exotel import ExotelFrameSerializer

        serializer = ExotelFrameSerializer(
            stream_sid="MZ_test",
            params=ExotelFrameSerializer.InputParams(
                exotel_sample_rate=8000,
                sample_rate=16000,
            ),
        )
        await serializer.setup(StartFrame(audio_in_sample_rate=16000))

        collected_pcm = bytearray()
        for _ in range(10):
            pcm_at_16k = _generate_1khz_pcm(16000, 100)
            payload = base64.b64encode(pcm_at_16k).decode()
            exotel_msg = json.dumps(
                {
                    "event": "media",
                    "media": {"payload": payload},
                }
            )
            frame = await serializer.deserialize(exotel_msg)
            if frame is not None:
                collected_pcm.extend(frame.audio)

        output_samples = len(collected_pcm) // 2
        output_duration_ms = output_samples * 1000 / 16000
        ratio = output_duration_ms / 1000.0
        assert ratio > 1.5 or ratio < 0.6, (
            f"NEGATIVE CONTROL FAILED: ratio {ratio:.2f} is within "
            f"normal range — the duration probe cannot detect "
            f"double-speed. Expected ratio >1.5 or <0.6 for a "
            f"rate mismatch (declared 8kHz, fed 16kHz)."
        )


# ============================================================================
# Condition 4: Barge-in — InterruptionFrame → Exotel clear JSON
# ============================================================================


class TestBargeInClear:
    async def test_interruption_frame_emits_exotel_clear(self) -> None:
        """InterruptionFrame → {"event":"clear","streamSid":"..."}."""
        from pipecat.frames.frames import InterruptionFrame
        from pipecat.serializers.exotel import ExotelFrameSerializer

        serializer = ExotelFrameSerializer(stream_sid="MZ_barge")
        result = await serializer.serialize(InterruptionFrame())
        assert result is not None
        parsed = json.loads(result)
        assert parsed["event"] == "clear"
        assert parsed["streamSid"] == "MZ_barge"

    async def test_serializer_does_not_filter_stale_generation_audio(self) -> None:
        """Serializer produces audio regardless of generation — filtering
        stale generations is the pipeline's responsibility, not the
        serializer's."""
        from pipecat.frames.frames import OutputAudioRawFrame, StartFrame
        from pipecat.serializers.exotel import ExotelFrameSerializer

        serializer = ExotelFrameSerializer(
            stream_sid="MZ_stale",
            params=ExotelFrameSerializer.InputParams(
                exotel_sample_rate=16000,
                sample_rate=16000,
            ),
        )
        await serializer.setup(StartFrame(audio_in_sample_rate=16000))
        result = None
        for _ in range(5):
            result = await serializer.serialize(
                OutputAudioRawFrame(
                    audio=b"\x00" * 4800,
                    sample_rate=24000,
                    num_channels=1,
                )
            )
            if result is not None:
                break
        assert result is not None


# ============================================================================
# Condition 6: Start event validation
# ============================================================================


class TestStartEventValidation:
    def test_valid_start_event(self) -> None:
        msg = {
            "event": "start",
            "start": {
                "stream_sid": "MZ_test",
                "call_sid": "CA_test",
                "account_sid": "AC_test",
                "from": "+919000000001",
                "to": "08012345678",
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": "16000",
                    "bit_rate": "16",
                },
            },
        }
        metadata = validate_start_event(msg)
        assert metadata.stream_sid == "MZ_test"
        assert metadata.call_sid == "CA_test"
        assert metadata.sample_rate == 16000

    def test_missing_start_payload(self) -> None:
        with pytest.raises(ExotelStartValidationError, match="missing"):
            validate_start_event({"event": "start"})

    def test_missing_stream_sid(self) -> None:
        msg = {
            "event": "start",
            "start": {
                "stream_sid": "",
                "call_sid": "CA",
                "account_sid": "AC",
                "from": "+919000000001",
                "to": "08012345678",
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": "16000",
                },
            },
        }
        with pytest.raises(ExotelStartValidationError, match="stream_sid"):
            validate_start_event(msg)

    def test_missing_media_format(self) -> None:
        msg = {
            "event": "start",
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "account_sid": "AC",
                "from": "+919000000001",
                "to": "08012345678",
            },
        }
        with pytest.raises(ExotelStartValidationError, match="media_format"):
            validate_start_event(msg)

    def test_unsupported_codec(self) -> None:
        msg = {
            "event": "start",
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "account_sid": "AC",
                "from": "+919000000001",
                "to": "08012345678",
                "media_format": {
                    "encoding": "audio/opus",
                    "sample_rate": "16000",
                },
            },
        }
        with pytest.raises(ExotelStartValidationError, match="codec"):
            validate_start_event(msg)

    def test_missing_sample_rate(self) -> None:
        msg = {
            "event": "start",
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "account_sid": "AC",
                "from": "+919000000001",
                "to": "08012345678",
                "media_format": {
                    "encoding": "audio/x-raw",
                },
            },
        }
        with pytest.raises(ExotelStartValidationError, match="sample_rate"):
            validate_start_event(msg)

    def test_unsupported_sample_rate(self) -> None:
        msg = {
            "event": "start",
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "account_sid": "AC",
                "from": "+919000000001",
                "to": "08012345678",
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": "44100",
                },
            },
        }
        with pytest.raises(ExotelStartValidationError, match=r"unsupported.*sample_rate"):
            validate_start_event(msg)

    def test_malformed_sample_rate(self) -> None:
        msg = {
            "event": "start",
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "account_sid": "AC",
                "from": "+919000000001",
                "to": "08012345678",
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": "not_a_number",
                },
            },
        }
        with pytest.raises(ExotelStartValidationError, match="malformed"):
            validate_start_event(msg)

    @pytest.mark.parametrize("rate", [8000, 16000, 24000])
    def test_all_supported_rates_accepted(self, rate: int) -> None:
        msg = {
            "event": "start",
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "account_sid": "AC",
                "from": "+919000000001",
                "to": "08012345678",
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": str(rate),
                },
            },
        }
        metadata = validate_start_event(msg)
        assert metadata.sample_rate == rate


# ============================================================================
# Rate drift detection
# ============================================================================


class TestRateDrift:
    def test_no_drift_when_rates_match(self) -> None:
        assert check_rate_drift(16000, 16000) is False

    def test_drift_when_rates_differ(self) -> None:
        assert check_rate_drift(8000, 16000) is True

    def test_drift_alerts_both_directions(self) -> None:
        assert check_rate_drift(24000, 16000) is True
        assert check_rate_drift(16000, 24000) is True


# ============================================================================
# Condition 7: No custom codec path production-reachable
# ============================================================================


class TestNoDeadCodePath:
    def test_exotel_audio_deleted(self) -> None:
        """Custom codec path exotel_audio.py is deleted, not retained."""
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("fonely.api.channels.exotel_audio")

    def test_media_py_deleted(self) -> None:
        """Custom media event model media.py is deleted, not retained."""
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("fonely.domain.calls.media")


# ============================================================================
# WS message parsing
# ============================================================================


class TestWSMessageParsing:
    def test_valid_json(self) -> None:
        msg = parse_ws_start_message('{"event": "start"}')
        assert msg["event"] == "start"

    def test_oversized_rejected(self) -> None:
        with pytest.raises(ExotelStartValidationError, match="too large"):
            parse_ws_start_message("x" * 20_000)

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(ExotelStartValidationError, match="invalid"):
            parse_ws_start_message("not json")

    def test_non_object_rejected(self) -> None:
        with pytest.raises(ExotelStartValidationError, match="expected JSON"):
            parse_ws_start_message("[1,2]")
