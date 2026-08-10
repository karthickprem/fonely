"""Tests for bounded voice telemetry exporter."""
from fonely.voice.telemetry import VoiceTelemetryExporter


def test_emit_and_drain():
    t = VoiceTelemetryExporter("sess-1", max_size=10)
    t.emit("test_event", key="value")
    events = t.drain()
    assert len(events) == 1
    assert events[0].name == "test_event"
    assert events[0].data["key"] == "value"
    assert events[0].session_id == "sess-1"


def test_bounded_queue_drops_oldest():
    t = VoiceTelemetryExporter("sess-1", max_size=3)
    for i in range(5):
        t.emit(f"event_{i}")
    events = t.drain()
    assert len(events) == 3
    assert events[0].name == "event_2"
    assert t._dropped == 2


def test_usage_tracking():
    t = VoiceTelemetryExporter("sess-1")
    t.record_stt_usage(1.5)
    t.record_stt_usage(2.0)
    t.record_llm_usage(100, 50)
    t.record_tts_usage(200)
    summary = t.usage_summary()
    assert summary["stt_seconds"] == 3.5
    assert summary["llm_input_tokens"] == 100
    assert summary["llm_output_tokens"] == 50
    assert summary["tts_characters"] == 200


def test_close_idempotent():
    t = VoiceTelemetryExporter("sess-1")
    s1 = t.close()
    s2 = t.close()
    assert s1 == s2


def test_close_includes_telemetry_closed_event():
    t = VoiceTelemetryExporter("sess-1")
    t.close()
    events = t.drain()
    assert any(e.name == "session_telemetry_closed" for e in events)


def test_no_emit_after_close():
    t = VoiceTelemetryExporter("sess-1")
    t.close()
    t.emit("should_not_appear")
    events = t.drain()
    assert all(e.name != "should_not_appear" for e in events)
