"""Tests for live run preparation and credential validation."""
from fonely.voice.runbook import (
    RecordingPaths,
    validate_credentials,
    LIVE_RUN_CHECKLIST,
    NATIVE_REVIEW_PROCEDURE,
)


def test_validate_credentials_reports_unset():
    ready, results = validate_credentials()
    for r in results:
        assert r["name"] in {"SARVAM_API_KEY", "ANTHROPIC_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID"}
        assert r["status"] in {"SET", "UNSET", "SET_SHORT"}
        assert "value" not in str(r).lower()


def test_recording_paths_structured():
    paths = RecordingPaths.for_session("test-session-1")
    assert "test-session-1" in paths.base_dir
    assert paths.transcript_path.endswith("transcript.json")
    assert paths.evidence_path.endswith("evidence.json")
    assert paths.telemetry_path.endswith("telemetry.jsonl")
    assert paths.native_review_path.endswith("native-review.json")


def test_recording_paths_immutable():
    paths = RecordingPaths.for_session("test")
    try:
        paths.session_id = "changed"
        assert False, "should be frozen"
    except AttributeError:
        pass


def test_checklist_has_required_sections():
    assert "CREDENTIALS" in LIVE_RUN_CHECKLIST
    assert "BROWSER" in LIVE_RUN_CHECKLIST
    assert "RECORDING" in LIVE_RUN_CHECKLIST
    assert "EVIDENCE" in LIVE_RUN_CHECKLIST
    assert "BOUNDARIES" in LIVE_RUN_CHECKLIST
    assert "FailClosedValidatorStub" in LIVE_RUN_CHECKLIST


def test_native_review_procedure_complete():
    assert "NATURALNESS" in NATIVE_REVIEW_PROCEDURE
    assert "CODE-SWITCHING" in NATIVE_REVIEW_PROCEDURE
    assert "PRONUNCIATION" in NATIVE_REVIEW_PROCEDURE
    assert "RESPONSE QUALITY" in NATIVE_REVIEW_PROCEDURE
    assert "1-5 scale" in NATIVE_REVIEW_PROCEDURE


def test_no_credential_values_in_source():
    import inspect
    import fonely.voice.runbook as module
    source = inspect.getsource(module)
    assert "sk-" not in source
    assert "api_key=" not in source.lower() or "os.environ" in source
