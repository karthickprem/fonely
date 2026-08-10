"""Tests for typed voice session configuration."""
from fonely.voice.config import (
    CONSEQUENTIAL_CLASSES,
    AudioConfig,
    LLMConfig,
    STTConfig,
    SessionLimits,
    SessionState,
    SpeechClass,
    TTSConfig,
    TurnConfig,
    VADConfig,
    VoiceSessionConfig,
    _VALID_TRANSITIONS,
)


def test_speech_class_consequential_set():
    assert SpeechClass.NON_CONSEQUENTIAL not in CONSEQUENTIAL_CLASSES
    assert SpeechClass.COMMITTED_CREATE in CONSEQUENTIAL_CLASSES
    assert SpeechClass.NOTIFICATION_SENT in CONSEQUENTIAL_CLASSES
    assert SpeechClass.HANDOFF_CONNECTED in CONSEQUENTIAL_CLASSES
    assert SpeechClass.REVIEWED_MEDICAL in CONSEQUENTIAL_CLASSES
    assert len(CONSEQUENTIAL_CLASSES) == len(SpeechClass) - 1


def test_session_state_transitions():
    assert SessionState.SIGNALING in _VALID_TRANSITIONS[SessionState.CREATED]
    assert SessionState.FAILED in _VALID_TRANSITIONS[SessionState.CREATED]
    assert SessionState.ACTIVE in _VALID_TRANSITIONS[SessionState.CONNECTING]
    assert SessionState.RECONNECTING in _VALID_TRANSITIONS[SessionState.ACTIVE]
    assert SessionState.DRAINING in _VALID_TRANSITIONS[SessionState.ACTIVE]
    assert len(_VALID_TRANSITIONS[SessionState.CLOSED]) == 0
    assert len(_VALID_TRANSITIONS[SessionState.FAILED]) == 0


def test_config_defaults():
    cfg = VoiceSessionConfig(session_id="test-1", business_id=1)
    assert cfg.stt.model == "saaras:v3"
    assert cfg.llm.model == "claude-opus-4-6"
    assert cfg.tts.model == "sonic-3.5"
    assert cfg.audio.input_sample_rate == 16000
    assert cfg.audio.output_sample_rate == 24000
    assert cfg.vad.confidence == 0.70
    assert cfg.limits.max_duration_seconds == 600
    assert cfg.limits.idle_timeout_seconds == 300


def test_config_immutable():
    cfg = VoiceSessionConfig(session_id="test-1", business_id=1)
    try:
        cfg.session_id = "changed"
        assert False, "should be frozen"
    except AttributeError:
        pass
