import asyncio
import hashlib
import json
import sys
import wave
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

LAB = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB))

from voice_eval.audio import inspect_wav, verify_fixture_audio
from voice_eval.contracts import apply_annotations, load_json_schema, validate_manifest, validate_report
from voice_eval.correction import propose_shadow_correction
from voice_eval.metrics import nearest_rank_percentile, score_critical_entities, word_error_counts
from voice_eval.observer import VoiceEvalObserver
from voice_eval.cli import safe_output_path
from voice_eval.server import create_app
from pipecat.frames.frames import TranscriptionFrame, TTSAudioRawFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection


def write_wav(path: Path, frames=16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b'\0\0' * frames)


def fixture_record(root: Path):
    audio = root / 'audio' / 'fixture.wav'; write_wav(audio)
    meta = inspect_wav(audio)
    return {"schema_version":1,"fixture_id":"VF-TAM-0001","audio":{"relative_path":"audio/fixture.wav","sha256":meta.sha256,"media_type":"audio/wav","sample_rate_hz":16000,"channels":1,"sample_width_bits":16,"duration_ms":1000},"locale":"ta-IN","speech_style":"tanglish","condition":"clean","split":"development","speaker_id":"synthetic-1","reference":{"transcript":"எனக்கு doctor appointment வேணும்","intent_label":"book_appointment","critical_entities":[{"kind":"intent","value":"doctor appointment","variants":["appointment"],"critical":True}]},"oracle_refs":[],"provenance":{"source":"synthetic_tts","consent_template_version":None,"consent_status":"synthetic","license":"internal synthetic fixture","commercial_use":False,"derivative_model_use":False},"annotation":{"language_review_status":"synthetic","domain_review_status":"unreviewed","annotation_status":"draft","annotator_roles":[],"revision":1},"notes":"synthetic"}


def test_schemas_are_valid():
    for name in ['audio-fixture-manifest.v1.schema.json','voice-run-result.v1.schema.json','voice-eval-report.v1.schema.json']:
        Draft202012Validator.check_schema(load_json_schema(name))


def test_manifest_and_wav_validation(tmp_path):
    record = fixture_record(tmp_path)
    manifest = tmp_path / 'manifests' / 'v1.jsonl'; manifest.parent.mkdir(); manifest.write_text(json.dumps(record,ensure_ascii=False)+'\n')
    assert validate_manifest(manifest,tmp_path)[0]['fixture_id']=='VF-TAM-0001'
    record['audio']['sha256']='0'*64; manifest.write_text(json.dumps(record)+'\n')
    with pytest.raises(ValueError,match='sha256 mismatch'): validate_manifest(manifest,tmp_path)


def test_real_recording_is_rejected(tmp_path):
    record=fixture_record(tmp_path); record['provenance']['source']='human_recording'; record['provenance']['consent_status']='approved'
    manifest=tmp_path/'m.jsonl'; manifest.write_text(json.dumps(record)+'\n')
    with pytest.raises(ValueError,match='rejects'): validate_manifest(manifest,tmp_path)


def test_output_path_must_remain_under_data_root(tmp_path):
    inside = tmp_path / "runs" / "run.jsonl"
    inside.parent.mkdir()
    assert safe_output_path(tmp_path, inside) == inside
    with pytest.raises(ValueError):
        safe_output_path(tmp_path, tmp_path.parent / "leak.jsonl")


def test_known_wer_counts():
    counts=word_error_counts('doctor appointment tomorrow','documentary tomorrow','en-IN')
    assert (counts.substitutions,counts.insertions,counts.deletions,counts.reference_words)==(1,0,1,3)
    assert counts.wer==pytest.approx(2/3)
    assert nearest_rank_percentile([10,20,30,40],95)==40


def test_entity_scoring():
    entities=[{"kind":"doctor","value":"Dr. Priya","variants":["Priya"],"critical":True}]
    assert score_critical_entities(entities,'Doctor Priya நாளைக்கு available','ta-IN')==(1,1)
    assert score_critical_entities(entities,'Dr. Priyanka நாளைக்கு available','ta-IN')==(0,1)
    entities.append({"kind":"service","value":"checkup","variants":[],"critical":False})
    assert score_critical_entities(entities,'Doctor Priya available','ta-IN')==(1,1)


def test_shadow_correction_and_critical_clarification():
    safe=propose_shadow_correction('DOCUMENTARY at aminji karai',[])
    assert safe.decision=='would_correct'
    assert safe.proposed_transcript=='doctor appointment at Aminjikarai'
    intent=propose_shadow_correction('எனக்கு documentary book பண்ணனும்',[{"kind":"intent","value":"doctor appointment","variants":[],"critical":True}])
    assert intent.decision=='would_clarify' and intent.proposed_transcript=='எனக்கு documentary book பண்ணனும்'
    critical=propose_shadow_correction('root channel venum',[{"kind":"service","value":"root canal","variants":[],"critical":True}])
    assert critical.decision=='would_clarify' and critical.proposed_transcript=='root channel venum'


def test_observer_writes_sanitized_deduplicated_events(tmp_path):
    async def run():
        output = tmp_path / "telemetry" / "session.jsonl"
        observer = VoiceEvalObserver(output_path=output, session_id="opaque-session")
        await observer.on_pipeline_started()
        transcript = TranscriptionFrame(
            text="Patient Karthick phone 9999999999",
            user_id="",
            timestamp="2026-08-03T00:00:00Z",
        )
        audio = TTSAudioRawFrame(audio=b"\x01\x02" * 100, sample_rate=24000, num_channels=1)
        for frame in [transcript, transcript, audio]:
            await observer.on_push_frame(
                FramePushed(None, None, frame, FrameDirection.DOWNSTREAM, 0)
            )
        await observer.close()
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        assert [row["event"] for row in rows].count("stt_transcript") == 1
        assert [row["event"] for row in rows].count("tts_audio") == 1
        serialized = json.dumps(rows)
        assert "Karthick" not in serialized and "9999999999" not in serialized
        assert "audio" not in rows[2]

    asyncio.run(run())


def test_loopback_ui_defaults_disabled_and_saves_external_annotation(tmp_path):
    async def run():
        import httpx

        record = fixture_record(tmp_path)
        manifest = tmp_path / "manifests" / "foundation-v1.jsonl"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps(record, ensure_ascii=False) + "\n")
        app = create_app(tmp_path, "disabled", "test-token")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"x-voice-eval-token": "test-token"},
        ) as client:
            config = (await client.get("/api/config")).json()
            assert config["collection_mode"] == "disabled"
            unauthorized = await client.get("/api/config", headers={"x-voice-eval-token": "wrong"})
            assert unauthorized.status_code == 401
            response = await client.patch(
                "/api/fixtures/VF-TAM-0001/annotation",
                json={
                    "transcript": record["reference"]["transcript"],
                    "language_mix": "tanglish",
                    "condition": "clean",
                    "intent": "book_appointment",
                    "reviewer_role": "internal_rnd",
                    "reviewed_revision": 1,
                },
            )
            assert response.status_code == 200
            saved = json.loads((tmp_path / "annotations" / "VF-TAM-0001.json").read_text())
            assert saved["prior_review_invalidated"] is True
            assert saved["revision"] == 2
            stale = await client.patch(
                "/api/fixtures/VF-TAM-0001/annotation",
                json={
                    "transcript": "stale overwrite",
                    "language_mix": "tanglish",
                    "condition": "clean",
                    "intent": "book_appointment",
                    "reviewer_role": "other",
                    "reviewed_revision": 1,
                },
            )
            assert stale.status_code == 409
            listed = (await client.get("/api/fixtures")).json()[0]
            assert listed["annotation"]["revision"] == 2
            effective = apply_annotations(validate_manifest(manifest, tmp_path), tmp_path)[0]
            assert effective["annotation"]["revision"] == 2
            assert effective["reference"]["transcript"] == record["reference"]["transcript"]

    asyncio.run(run())


def test_collection_is_disabled_by_default(tmp_path):
    async def run():
        import base64
        import httpx

        audio = tmp_path / "source.wav"
        write_wav(audio)
        app = create_app(tmp_path, "disabled", "test-token")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers={"x-voice-eval-token": "test-token"}) as client:
            response = await client.post("/api/fixtures", json={
                "audio_base64": base64.b64encode(audio.read_bytes()).decode(),
                "transcript": "synthetic appointment request",
                "locale": "en-IN", "speech_style": "indian_english", "condition": "clean",
                "internal_rnd_speaker": True, "synthetic_scenario_only": True,
                "no_real_person_data": True, "local_external_storage": True,
                "consent_template_version": "voice-rd-consent-v1-draft"
            })
            assert response.status_code == 403

    asyncio.run(run())


def test_synthetic_loopback_upload_is_validated_and_external(tmp_path):
    async def run():
        import base64
        import httpx

        audio = tmp_path / "source.wav"
        write_wav(audio)
        app = create_app(tmp_path, "synthetic_loopback", "test-token")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers={"x-voice-eval-token": "test-token"}) as client:
            response = await client.post("/api/fixtures", json={
                "audio_base64": base64.b64encode(audio.read_bytes()).decode(),
                "transcript": "synthetic appointment request",
                "locale": "en-IN", "speech_style": "indian_english", "condition": "clean",
                "intent_label": "book_appointment", "internal_rnd_speaker": True,
                "synthetic_scenario_only": True, "no_real_person_data": True,
                "local_external_storage": True,
                "consent_template_version": "voice-rd-consent-v1-draft"
            })
            assert response.status_code == 200, response.text
            record = response.json()
            assert (tmp_path / record["audio"]["relative_path"]).exists()
            validate_manifest(tmp_path / "manifests" / "foundation-v1.jsonl", tmp_path)

    asyncio.run(run())


def test_loopback_server_rejects_real_collection(tmp_path):
    with pytest.raises(ValueError):
        create_app(tmp_path, "real_collection")
