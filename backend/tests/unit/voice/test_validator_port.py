"""Tests for fail-closed validator port and stub."""
from fonely.voice.config import SpeechClass
from fonely.voice.validator_port import (
    FailClosedValidatorStub,
    ValidationDecision,
)


def test_stub_blocks_all_consequential():
    stub = FailClosedValidatorStub()
    for cls in SpeechClass:
        if cls == SpeechClass.NON_CONSEQUENTIAL:
            continue
        result = stub.validate_speech("any text", cls)
        assert result.decision == ValidationDecision.BLOCK, f"{cls} should block"
        assert "consequential" in result.reason


def test_stub_allows_non_consequential():
    stub = FailClosedValidatorStub()
    result = stub.validate_speech("What date works?", SpeechClass.NON_CONSEQUENTIAL)
    assert result.decision == ValidationDecision.ALLOW


def test_stub_source_is_stub():
    stub = FailClosedValidatorStub()
    result = stub.validate_speech("text", SpeechClass.NON_CONSEQUENTIAL)
    assert result.source == "stub"
