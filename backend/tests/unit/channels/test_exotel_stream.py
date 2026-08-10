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
    async def test_1khz_survives_serializer_roundtrip(
        self, rate: int
    ) -> None:
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
        for chunk_idx in range(3):
            pcm_chunk = _generate_1khz_pcm(rate, 100)
            payload = base64.b64encode(pcm_chunk).decode()
            exotel_msg = json.dumps({
                "event": "media",
                "media": {"payload": payload},
            })
            frame = await serializer.deserialize(exotel_msg)
            if frame is not None:
                assert frame.sample_rate == 16000
                collected_pcm.extend(frame.audio)

        assert len(collected_pcm) >= 640, (
            f"expected output audio, got {len(collected_pcm)} bytes"
        )

        freq = _measure_dominant_frequency(bytes(collected_pcm), 16000)
        assert 800 < freq < 1200, (
            f"expected ~1000Hz, got {freq:.0f}Hz at input rate {rate}"
        )

    @pytest.mark.parametrize("rate", [8000, 16000, 24000])
    async def test_duration_preserved(self, rate: int) -> None:
        """300ms of audio at declared rate produces ~300ms at output rate.

        Send 3x100ms chunks to account for resampler buffering.
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
            exotel_msg = json.dumps({
                "event": "media",
                "media": {"payload": payload},
            })
            frame = await serializer.deserialize(exotel_msg)
            if frame is not None:
                collected_pcm.extend(frame.audio)

        output_samples = len(collected_pcm) // 2
        output_duration_ms = output_samples * 1000 / 16000
        # Stream resampler buffers ~100ms initially; over 1s of audio
        # the ratio should be close to 1.0
        ratio = output_duration_ms / total_input_ms
        assert 0.8 < ratio < 1.2, (
            f"duration ratio {ratio:.2f} at input rate {rate}: "
            f"expected ~{total_input_ms}ms, got {output_duration_ms:.1f}ms"
        )


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
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": "16000",
                    "bit_rate": "16",
                },
            },
        }
        sid, cid, rate = validate_start_event(msg)
        assert sid == "MZ_test"
        assert cid == "CA_test"
        assert rate == 16000

    def test_missing_start_payload(self) -> None:
        with pytest.raises(ExotelStartValidationError, match="missing"):
            validate_start_event({"event": "start"})

    def test_missing_stream_sid(self) -> None:
        msg = {
            "start": {
                "stream_sid": "",
                "call_sid": "CA",
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": "16000",
                },
            }
        }
        with pytest.raises(ExotelStartValidationError, match="stream_sid"):
            validate_start_event(msg)

    def test_missing_media_format(self) -> None:
        msg = {
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
            }
        }
        with pytest.raises(ExotelStartValidationError, match="media_format"):
            validate_start_event(msg)

    def test_unsupported_codec(self) -> None:
        msg = {
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "media_format": {
                    "encoding": "audio/opus",
                    "sample_rate": "16000",
                },
            }
        }
        with pytest.raises(ExotelStartValidationError, match="codec"):
            validate_start_event(msg)

    def test_missing_sample_rate(self) -> None:
        msg = {
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "media_format": {
                    "encoding": "audio/x-raw",
                },
            }
        }
        with pytest.raises(
            ExotelStartValidationError, match="sample_rate"
        ):
            validate_start_event(msg)

    def test_unsupported_sample_rate(self) -> None:
        msg = {
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": "44100",
                },
            }
        }
        with pytest.raises(
            ExotelStartValidationError, match="unsupported.*sample_rate"
        ):
            validate_start_event(msg)

    def test_malformed_sample_rate(self) -> None:
        msg = {
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": "not_a_number",
                },
            }
        }
        with pytest.raises(
            ExotelStartValidationError, match="malformed"
        ):
            validate_start_event(msg)

    @pytest.mark.parametrize("rate", [8000, 16000, 24000])
    def test_all_supported_rates_accepted(self, rate: int) -> None:
        msg = {
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": str(rate),
                },
            }
        }
        _, _, parsed_rate = validate_start_event(msg)
        assert parsed_rate == rate


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
    def test_no_production_import_of_exotel_audio(self) -> None:
        """No production module imports exotel_audio."""
        import importlib
        import pkgutil

        import fonely.api.channels as channels_pkg
        import fonely.domain as domain_pkg
        import fonely.services as services_pkg

        for pkg in [channels_pkg, domain_pkg, services_pkg]:
            for importer, name, _ispkg in pkgutil.walk_packages(
                pkg.__path__, prefix=pkg.__name__ + "."
            ):
                if "exotel_audio" in name:
                    continue
                if "test" in name:
                    continue
                try:
                    mod = importlib.import_module(name)
                except ImportError:
                    continue
                source = getattr(mod, "__file__", "") or ""
                if "exotel_audio" in source:
                    continue
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name, None)
                    obj_module = getattr(obj, "__module__", "")
                    assert "exotel_audio" not in obj_module, (
                        f"{name}.{attr_name} imports from exotel_audio"
                    )


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
        with pytest.raises(
            ExotelStartValidationError, match="expected JSON"
        ):
            parse_ws_start_message("[1,2]")
