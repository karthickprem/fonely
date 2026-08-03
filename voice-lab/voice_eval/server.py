from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from voice_eval.audio import inspect_wav
from voice_eval.contracts import _validate_records, validate_manifest

STATIC = Path(__file__).parent / "static"
CONSENT_VERSION = "voice-rd-consent-v1-draft"


class SyntheticFixtureCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_base64: str
    transcript: str
    locale: str
    speech_style: str
    condition: str
    intent_label: str | None = None
    internal_rnd_speaker: bool
    synthetic_scenario_only: bool
    no_real_person_data: bool
    local_external_storage: bool
    consent_template_version: str


class AnnotationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transcript: str
    language_mix: str
    condition: str
    intent: str | None = None
    reviewer_role: str
    reviewed_revision: int


def create_app(data_root: Path, collection_mode: str = "disabled", access_token: str | None = None) -> FastAPI:
    root = data_root.resolve(strict=True)
    worktree = Path(__file__).resolve().parents[2]
    if root == worktree or root.is_relative_to(worktree):
        raise ValueError("data root must be outside Git worktree")
    if collection_mode not in {"disabled", "synthetic_loopback"}:
        raise ValueError("V1 supports only disabled or synthetic_loopback")
    token = access_token or os.environ.get("VOICE_EVAL_ACCESS_TOKEN") or uuid.uuid4().hex
    app = FastAPI(docs_url=None, redoc_url=None)
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    def manifest_path() -> Path:
        return root / "manifests" / "foundation-v1.jsonl"

    def safe_directory(name: str) -> Path:
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        resolved = directory.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise HTTPException(500, f"unsafe {name} directory")
        return resolved

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        host = request.url.hostname or ""
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return __import__("fastapi").responses.JSONResponse({"detail": "loopback host required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {"http://127.0.0.1", "http://localhost", "http://testserver"} and not origin.startswith(("http://127.0.0.1:", "http://localhost:")):
            return __import__("fastapi").responses.JSONResponse({"detail": "invalid origin"}, status_code=403)
        if request.url.path.startswith("/api/") and request.headers.get("x-voice-eval-token") != token:
            return __import__("fastapi").responses.JSONResponse({"detail": "invalid token"}, status_code=401)
        return await call_next(request)

    @app.get("/")
    async def index(): return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    async def config(): return {"collection_mode": collection_mode, "allowed_source": "synthetic_loopback" if collection_mode == "synthetic_loopback" else None, "consent_template_version": CONSENT_VERSION, "founder_approval_required": True}

    @app.get("/api/fixtures")
    async def fixtures():
        manifest = manifest_path()
        if not manifest.exists(): return []
        rendered = []
        for source_row in validate_manifest(manifest, root):
            row = source_row.copy()
            annotation_path = root / "annotations" / f"{row['fixture_id']}.json"
            if annotation_path.exists():
                annotation = json.loads(annotation_path.read_text())
                row["reference"] = {**row["reference"], "transcript": annotation["transcript"], "intent_label": annotation.get("intent")}
                row["annotation"] = {**row["annotation"], "revision": annotation["revision"], "annotation_status": annotation["review_status"]}
            rendered.append(row)
        return rendered

    @app.get("/api/fixtures/{fixture_id}/audio")
    async def fixture_audio(fixture_id: str):
        rows = await fixtures()
        fixture = next((row for row in rows if row["fixture_id"] == fixture_id), None)
        if not fixture: raise HTTPException(404)
        path = (root / fixture["audio"]["relative_path"]).resolve()
        if not path.is_relative_to(root): raise HTTPException(403)
        return FileResponse(path, media_type="audio/wav")

    @app.post("/api/fixtures")
    async def create_fixture(request: SyntheticFixtureCreate):
        if collection_mode != "synthetic_loopback":
            raise HTTPException(403, "collection is disabled")
        attestations = [
            request.internal_rnd_speaker,
            request.synthetic_scenario_only,
            request.no_real_person_data,
            request.local_external_storage,
            request.consent_template_version == CONSENT_VERSION,
        ]
        if not all(attestations):
            raise HTTPException(400, "all synthetic-loopback attestations are required")
        if __import__("re").search(r"(?<!\d)\d{10,12}(?!\d)", request.transcript):
            raise HTTPException(400, "phone-like values are not allowed")
        try:
            audio = base64.b64decode(request.audio_base64, validate=True)
        except Exception as exc:
            raise HTTPException(400, "invalid base64 audio") from exc
        if not 3200 <= len(audio) <= 2_000_000:
            raise HTTPException(400, "audio size is outside V1 bounds")
        fixture_id = f"VF-LOOP-{uuid.uuid4().hex[:12].upper()}"
        relative_path = f"audio/{fixture_id}.wav"
        target = safe_directory("audio") / f"{fixture_id}.wav"
        temporary = target.with_suffix(".wav.tmp")
        temporary.write_bytes(audio)
        try:
            metadata = inspect_wav(temporary)
            if not 100 <= metadata.duration_ms <= 30000:
                raise ValueError("duration is outside V1 bounds")
            temporary.replace(target)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc
        record = {
            "schema_version": 1,
            "fixture_id": fixture_id,
            "audio": {"relative_path": relative_path, "sha256": metadata.sha256, "media_type": "audio/wav", "sample_rate_hz": metadata.sample_rate_hz, "channels": metadata.channels, "sample_width_bits": metadata.sample_width_bits, "duration_ms": metadata.duration_ms},
            "locale": request.locale,
            "speech_style": request.speech_style,
            "condition": request.condition,
            "split": "development",
            "speaker_id": "internal-rnd",
            "reference": {"transcript": request.transcript, "intent_label": request.intent_label, "critical_entities": []},
            "oracle_refs": [],
            "provenance": {"source": "synthetic_loopback", "consent_template_version": CONSENT_VERSION, "consent_status": "draft_internal", "license": "internal synthetic R&D only", "commercial_use": False, "derivative_model_use": False},
            "annotation": {"language_review_status": "unreviewed", "domain_review_status": "unreviewed", "annotation_status": "draft", "annotator_roles": ["internal_rnd"], "revision": 1},
            "notes": "Synthetic loopback fixture; not approved for production evidence",
        }
        try:
            _validate_records([record], "audio-fixture-manifest.v1.schema.json")
            safe_directory("manifests")
            manifest = manifest_path()
            with manifest.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return record

    @app.post("/api/manifests/validate")
    async def validate_current_manifest():
        manifest = manifest_path()
        if not manifest.exists():
            raise HTTPException(404, "no manifest")
        rows = validate_manifest(manifest, root)
        return {"valid": True, "fixtures": len(rows)}

    @app.delete("/api/fixtures/{fixture_id}")
    async def delete_fixture(fixture_id: str):
        if collection_mode != "synthetic_loopback":
            raise HTTPException(403, "collection is disabled")
        manifest = manifest_path()
        if not manifest.exists():
            raise HTTPException(404)
        rows = validate_manifest(manifest, root)
        fixture = next((row for row in rows if row["fixture_id"] == fixture_id), None)
        if not fixture:
            raise HTTPException(404)
        if fixture["provenance"]["source"] not in {"synthetic_tts", "synthetic_loopback"}:
            raise HTTPException(403, "V1 deletes synthetic fixtures only")
        remaining = [row for row in rows if row["fixture_id"] != fixture_id]
        temporary = manifest.with_suffix(".jsonl.tmp")
        temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in remaining))
        temporary.replace(manifest)
        audio_path = (root / fixture["audio"]["relative_path"]).resolve(strict=False)
        if audio_path.is_relative_to(root):
            audio_path.unlink(missing_ok=True)
        annotation_path = safe_directory("annotations") / f"{fixture_id}.json"
        annotation_path.unlink(missing_ok=True)
        return {"deleted": True, "fixture_id": fixture_id}

    @app.patch("/api/fixtures/{fixture_id}/annotation")
    async def annotate(fixture_id: str, update: AnnotationUpdate):
        rows = await fixtures()
        fixture = next((row for row in rows if row["fixture_id"] == fixture_id), None)
        if not fixture: raise HTTPException(404)
        target = safe_directory("annotations") / f"{fixture_id}.json"
        current_revision = fixture["annotation"]["revision"]
        if target.exists():
            current_revision = json.loads(target.read_text())["revision"]
        if update.reviewed_revision != current_revision:
            raise HTTPException(409, "annotation revision changed")
        payload = {**update.model_dump(), "fixture_id": fixture_id, "revision": current_revision + 1, "review_status": "draft", "prior_review_invalidated": True}
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(target)
        return payload

    return app
