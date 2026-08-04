import asyncio
import hashlib
import json
import sys
import wave
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

LAB = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB))

from voice_eval.analysis import classify_row, semantic_text
from voice_eval.audio import inspect_wav, verify_fixture_audio
from voice_eval.contracts import apply_annotations, load_json_schema, validate_manifest, validate_report
from voice_eval.correction import propose_shadow_correction
from voice_eval.costs import CostComponent, calculate_cost_per_verified_booking
from voice_eval.critical_fields import (
    FieldStatus,
    PhoneNumberState,
    apply_confirmation,
    collect_dtmf,
    collect_phone_attempt,
    grouped_phone_readback,
)
from voice_eval.evidence import write_immutable_jsonl
from voice_eval.metrics import (
    character_error_counts,
    nearest_rank_percentile,
    score_critical_entities,
    word_error_counts,
    wrong_script_characters,
)
from voice_eval.stt_contract import STTEvent, STTEventType
from voice_eval.observer import VoiceEvalObserver
from voice_eval.cli import safe_output_path
from voice_eval.server import create_app
from pipecat.frames.frames import TranscriptionFrame, TTSAudioRawFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection


def write_wav(path: Path, frames=16000):
    import math
    import struct

    path.parent.mkdir(parents=True, exist_ok=True)
    samples = b"".join(
        struct.pack("<h", int(0.5 * 32767 * math.sin(2 * math.pi * 440 * index / 16000)))
        for index in range(frames)
    )
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(samples)


def fixture_record(root: Path):
    audio = root / 'audio' / 'fixture.wav'; write_wav(audio)
    meta = inspect_wav(audio)
    return {"schema_version":1,"fixture_id":"VF-TAM-0001","audio":{"relative_path":"audio/fixture.wav","sha256":meta.sha256,"media_type":"audio/wav","sample_rate_hz":16000,"channels":1,"sample_width_bits":16,"duration_ms":1000},"locale":"ta-IN","speech_style":"tanglish","condition":"clean","split":"development","speaker_id":"synthetic-1","reference":{"transcript":"எனக்கு doctor appointment வேணும்","intent_label":"book_appointment","critical_entities":[{"kind":"intent","value":"doctor appointment","variants":["appointment"],"critical":True}]},"oracle_refs":[],"provenance":{"source":"synthetic_tts","consent_template_version":None,"consent_status":"synthetic","license":"internal synthetic fixture","commercial_use":False,"derivative_model_use":False},"annotation":{"language_review_status":"synthetic","domain_review_status":"unreviewed","annotation_status":"draft","annotator_roles":[],"revision":1},"notes":"synthetic"}


def test_schemas_are_valid():
    for name in ['audio-fixture-manifest.v1.schema.json','audio-fixture-manifest.v2.schema.json','voice-run-result.v1.schema.json','voice-eval-report.v1.schema.json']:
        Draft202012Validator.check_schema(load_json_schema(name))


def test_founder_prompt_pack_is_locked_and_balanced():
    from voice_eval.founder_studio import load_prompt_pack

    pack = load_prompt_pack()
    assert len(pack["prompts"]) == 50
    assert sum(prompt["speech_style"] == "tamil" for prompt in pack["prompts"]) == 25
    assert sum(prompt["speech_style"] == "tanglish" for prompt in pack["prompts"]) == 25


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


def test_character_error_and_raw_script_evidence():
    counts = character_error_counts("பல்", "கல்")
    assert counts.substitutions == 1
    assert wrong_script_characters("என் పేరు Karthik") == (4, 7)


def test_provider_neutral_stt_event_preserves_raw_evidence():
    event = STTEvent(
        schema_version=1,
        event_type=STTEventType.TRANSCRIPT_FINAL,
        session_id="session",
        turn_id="turn",
        generation_id="generation",
        provider="sarvam",
        model="saaras:v3",
        raw_text="ఎం పేరు Karthik",
        normalized_candidate="என் பெயர் Karthik",
        is_final=True,
    )
    record = event.to_record()
    assert record["event_type"] == "transcript_final"
    assert record["raw_text"] != record["normalized_candidate"]


def test_phone_attempts_replace_and_require_dtmf():
    state, act = collect_phone_attempt(PhoneNumberState(), "987654321")
    assert state.candidate == "987654321" and act.reason == "received_9_digits"
    state, act = collect_phone_attempt(state, "91234567890")
    assert state.candidate == "91234567890"
    assert state.status == FieldStatus.REQUIRE_DTMF
    assert act.act == "request_dtmf_phone"
    state, act = collect_dtmf(state, "12345 67890")
    assert act is None and state.status == FieldStatus.AWAITING_CONFIRMATION
    assert grouped_phone_readback(state.candidate) == ("12345", "67890")
    ambiguous = apply_confirmation(state, "ஆம் இல்லை")
    assert ambiguous.status == FieldStatus.AMBIGUOUS
    confirmed = apply_confirmation(state, "ஆம்")
    assert confirmed.authoritative_value == "1234567890"


def test_immutable_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "result.jsonl"
    write_immutable_jsonl(output, [{"result": 1}])
    with pytest.raises(FileExistsError):
        write_immutable_jsonl(output, [{"result": 2}])
    assert json.loads(output.read_text()) == {"result": 1}


def test_cost_per_success_fails_closed_on_unknown_cost():
    result = calculate_cost_per_verified_booking(
        [CostComponent("stt", None, "INR", None, None)],
        successful_bookings=1,
    )
    assert result.cost_per_verified_booking is None and result.evidence_gaps
    measured = calculate_cost_per_verified_booking(
        [CostComponent("stt", Decimal("10"), "INR", "invoice", "2026-08-04")],
        successful_bookings=2,
    )
    assert measured.cost_per_verified_booking == Decimal("5")


def test_entity_scoring():
    entities=[{"kind":"doctor","value":"Dr. Priya","variants":["Priya"],"critical":True}]
    assert score_critical_entities(entities,'Doctor Priya நாளைக்கு available','ta-IN')==(1,1)
    assert score_critical_entities(entities,'Dr. Priyanka நாளைக்கு available','ta-IN')==(0,1)
    entities.append({"kind":"service","value":"checkup","variants":[],"critical":False})
    assert score_critical_entities(entities,'Doctor Priya available','ta-IN')==(1,1)


def test_semantic_normalization_preserves_raw_evidence_but_equates_scripts():
    assert semantic_text("doctor appointment six thirty") == semantic_text("டாக்டர் அப்பாயின்ட்மென்ட் 6:30")
    assert semantic_text("Dr. Priya-வை பாக்கணும்") == semantic_text("Doctor Priyaவை பார்க்கணும்")


def test_shadow_correction_and_critical_clarification():
    safe=propose_shadow_correction('DOCUMENTARY at aminji karai',[])
    assert safe.decision=='would_correct'
    assert safe.proposed_transcript=='doctor appointment at Aminjikarai'
    intent=propose_shadow_correction('எனக்கு documentary book பண்ணனும்',[{"kind":"intent","value":"doctor appointment","variants":[],"critical":True}])
    assert intent.decision=='would_clarify' and intent.proposed_transcript=='எனக்கு documentary book பண்ணனும்'
    critical=propose_shadow_correction('root channel venum',[{"kind":"service","value":"root canal","variants":[],"critical":True}])
    assert critical.decision=='would_clarify' and critical.proposed_transcript=='root channel venum'
    observed=propose_shadow_correction('டாக்டர் பிரிய உபாகனம்',[])
    assert observed.decision=='would_clarify'
    assert observed.proposed_transcript=='டாக்டர் பிரிய உபாகனம்'


def test_observer_writes_sanitized_deduplicated_events(tmp_path):
    async def run():
        output = tmp_path / "telemetry" / "session.jsonl"
        observer = VoiceEvalObserver(output_path=output, session_id="opaque-session")
        await observer.on_pipeline_started()
        transcript = TranscriptionFrame(
            text="Synthetic caller phone 1111111111",
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
        assert "Synthetic caller" not in serialized and "1111111111" not in serialized
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


def test_founder_studio_requires_governance_and_persists_recording(tmp_path):
    async def run():
        import base64
        import httpx

        source = tmp_path / "source.wav"
        write_wav(source)
        app = create_app(tmp_path, "founder_recording", "test-token")
        headers = {"x-voice-eval-token": "test-token"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers=headers) as client:
            studio = (await client.get("/api/studio")).json()
            assert studio["total"] == 50 and studio["completed"] == 0
            prompt = studio["prompts"][0]
            unauthorized = await client.post("/api/recordings", json={})
            assert unauthorized.status_code == 422
            approval = (await client.post("/api/governance/founder-approval", json={
                "approver_name": "Karthick", "role": "founder", "allow_evaluation": True,
                "allow_named_providers": True, "allow_model_training": True,
                "prohibit_voice_cloning": True, "prohibit_real_person_data": True,
                "accept_90_day_retention": True,
            })).json()
            consent = (await client.post("/api/governance/participant-consent", json={
                "participant_name": "Karthick", "adult_self_recording": True,
                "alone_no_background_speakers": True, "fictional_prompts_only": True,
                "no_real_person_data": True, "allow_evaluation": True,
                "allow_named_providers": True, "allow_model_training": True,
                "understand_deletion_limit": True,
            })).json()
            payload = {
                "request_id": "request-12345678", "prompt_id": prompt["prompt_id"],
                "audio_base64": base64.b64encode(source.read_bytes()).decode(),
                "transcript": prompt["text"], "source_sample_rate_hz": 48000, "attempt": 1,
                "peak": 0.5, "clipped_ratio": 0.0, "speech_ratio": 0.5,
                "leading_silence_ms": 100, "trailing_silence_ms": 200,
                "approval_receipt_id": approval["receipt_id"], "consent_receipt_id": consent["receipt_id"],
            }
            response = await client.post("/api/recordings", json=payload)
            assert response.status_code == 200, response.text
            record = response.json()
            assert record["schema_version"] == 2
            assert record["provenance"]["voice_cloning_allowed"] is False
            assert (tmp_path / record["audio"]["relative_path"]).exists()
            # Idempotent retry returns the same fixture.
            assert (await client.post("/api/recordings", json=payload)).json()["fixture_id"] == record["fixture_id"]
            validate_manifest(tmp_path / "manifests" / "founder-chennai-v1.jsonl", tmp_path)
            progress = (await client.get("/api/studio")).json()
            assert progress["completed"] == 1
            deleted = await client.post(f"/api/recordings/{record['fixture_id']}/deletion-request", json={"confirm_fixture_id": record["fixture_id"], "reason": "test withdrawal"})
            assert deleted.status_code == 200
            assert not (tmp_path / record["audio"]["relative_path"]).exists()

    asyncio.run(run())


def test_explicit_internal_host_is_narrowly_allowed(tmp_path):
    async def run():
        import httpx

        app = create_app(tmp_path, "disabled", "token", "xhdctallapa40")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://xhdctallapa40:3049", headers={"x-voice-eval-token": "token", "origin": "http://xhdctallapa40:3049"}) as client:
            assert (await client.get("/api/config")).status_code == 200
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://other-host:3049", headers={"x-voice-eval-token": "token"}) as client:
            assert (await client.get("/api/config")).status_code == 403

    asyncio.run(run())


def test_loopback_server_rejects_real_collection(tmp_path):
    with pytest.raises(ValueError):
        create_app(tmp_path, "real_collection")
