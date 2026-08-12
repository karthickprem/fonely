"""Serializer construction from the admitted start event (V-lane step 4, T2).

The assertions read the rate off the CONSTRUCTED serializer — the value it will
actually decode inbound media with — NOT off the handoff we passed in. Asserting
``start.sample_rate == 16000`` after setting 16000 proves nothing; asserting the
serializer's own ``_exotel_sample_rate`` proves the value survived construction.

TestConstructionIsNotHardcoded mutation-proves this: it builds the serializer
through a variant that hardcodes 8000 and confirms the 16k/24k cases then FAIL,
so a real reintroduced literal would be caught.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pipecat.serializers.exotel import ExotelFrameSerializer

from fonely.voice.audio_runtime import build_serializer


@dataclass(frozen=True)
class _FakeStart:
    """Satisfies the MediaStreamStart Protocol for construction tests. Not an
    AudioSession — carries only wire parameters, no identity."""

    stream_sid: str
    provider_call_sid: str
    encoding: str
    sample_rate: int
    channels: int


def _start(rate: int) -> _FakeStart:
    return _FakeStart(
        stream_sid="stream-abc",
        provider_call_sid="pcs-123",
        encoding="l16",
        sample_rate=rate,
        channels=1,
    )


class TestSerializerRate:
    @pytest.mark.parametrize("rate", [8000, 16000, 24000])
    def test_constructed_serializer_decodes_at_declared_rate(self, rate: int):
        # Read the rate off the CONSTRUCTED serializer (what it decodes with),
        # not off the start we passed in.
        serializer = build_serializer(_start(rate))
        assert serializer._exotel_sample_rate == rate

    def test_stream_sid_quoted_from_typed_field(self):
        serializer = build_serializer(_start(16000))
        assert serializer._stream_sid == "stream-abc"

    @pytest.mark.parametrize("rate", [8000, 16000, 24000])
    def test_roundtrip_type(self, rate: int):
        assert isinstance(build_serializer(_start(rate)), ExotelFrameSerializer)


class TestConstructionIsNotHardcoded:
    """Mutation proof: if the construction path hardcoded a literal rate, the
    non-8000 cases would decode wrong. This proves the parametrization above is
    not decorative — a reintroduced literal is caught."""

    @staticmethod
    def _build_hardcoded_8000(start: _FakeStart) -> ExotelFrameSerializer:
        # The defect we are guarding against: ignore start.sample_rate, hardcode.
        return ExotelFrameSerializer(
            stream_sid=start.stream_sid,
            params=ExotelFrameSerializer.InputParams(
                exotel_sample_rate=8000,
                sample_rate=8000,
            ),
        )

    @pytest.mark.parametrize("rate", [16000, 24000])
    def test_hardcoded_path_fails_the_rate_assertion(self, rate: int):
        # The real build_serializer passes this assertion (proven above); the
        # hardcoded variant must FAIL it for 16k/24k — i.e. the assertion has
        # teeth.
        mutant = self._build_hardcoded_8000(_start(rate))
        assert mutant._exotel_sample_rate != rate  # would be 8000, not `rate`
        # And the real path passes where the mutant fails:
        assert build_serializer(_start(rate))._exotel_sample_rate == rate
