from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import uuid
import wave
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from voice_eval.audio import inspect_wav
from voice_eval.contracts import _validate_records

LAB = Path(__file__).resolve().parent.parent
POLICY_JSON = Path(__file__).resolve().parent / "policy" / "founder-self-recording.v1.json"
POLICY_MD = LAB.parent / "docs" / "qa" / "VOICE_RD_FOUNDER_POLICY.v1.md"
CONSENT_MD = LAB.parent / "docs" / "qa" / "VOICE_RD_FOUNDER_CONSENT.v1.md"
PROMPTS_JSON = Path(__file__).resolve().parent / "prompts" / "founder-chennai-v1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now() -> datetime:
    return datetime.now(timezone.utc)


def load_prompt_pack() -> dict:
    pack = json.loads(PROMPTS_JSON.read_text())
    prompts = pack.get("prompts", [])
    if len(prompts) != 50 or len({p["prompt_id"] for p in prompts}) != 50:
        raise ValueError("founder prompt pack must contain 50 unique prompts")
    if sum(p["speech_style"] == "tamil" for p in prompts) != 25 or sum(p["speech_style"] == "tanglish" for p in prompts) != 25:
        raise ValueError("founder prompt pack must be 25 Tamil and 25 Tanglish")
    serialized = json.dumps(prompts, ensure_ascii=False)
    if __import__("re").search(r"(?<!\d)\d{10,12}(?!\d)", serialized):
        raise ValueError("prompt pack contains phone-like data")
    return pack


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approver_name: str
    role: str
    allow_evaluation: bool
    allow_named_providers: bool
    allow_model_training: bool
    prohibit_voice_cloning: bool
    prohibit_real_person_data: bool
    accept_90_day_retention: bool


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    participant_name: str
    adult_self_recording: bool
    alone_no_background_speakers: bool
    fictional_prompts_only: bool
    no_real_person_data: bool
    allow_evaluation: bool
    allow_named_providers: bool
    allow_model_training: bool
    understand_deletion_limit: bool


class RecordingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    prompt_id: str
    audio_base64: str
    transcript: str
    source_sample_rate_hz: int
    attempt: int
    peak: float
    clipped_ratio: float
    speech_ratio: float
    leading_silence_ms: int
    trailing_silence_ms: int
    approval_receipt_id: str
    consent_receipt_id: str


class SkipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_fixture_id: str
    reason: str


class FounderStudio:
    def __init__(self, root: Path):
        self.root = root
        self.policy = json.loads(POLICY_JSON.read_text())
        self.policy_sha = sha256(POLICY_JSON)
        self.policy_doc_sha = sha256(POLICY_MD)
        self.consent_sha = sha256(CONSENT_MD)
        self.pack = load_prompt_pack()
        self.prompts = {p["prompt_id"]: p for p in self.pack["prompts"]}
        self.lock = asyncio.Lock()
        for name in ["governance/approvals", "governance/consents", "governance/deletion-requests", "recordings", "manifests", ".staging", ".trash", "studio/skips"]:
            path = (root / name); path.mkdir(parents=True, exist_ok=True)
            if not path.resolve().is_relative_to(root): raise ValueError("unsafe studio directory")
        for stage in (root / ".staging").iterdir():
            if stage.is_dir(): shutil.rmtree(stage)
        self.expire_recordings()
        self.rebuild_manifest()

    def receipt(self, folder: str, receipt_id: str) -> dict:
        path = self.root / "governance" / folder / f"{receipt_id}.json"
        if not path.exists(): raise HTTPException(403, f"missing {folder} receipt")
        return json.loads(path.read_text())

    def latest_receipt(self, folder: str) -> dict | None:
        files = sorted((self.root / "governance" / folder).glob("*.json"), key=lambda p: p.stat().st_mtime)
        return json.loads(files[-1].read_text()) if files else None

    def validate_governance(self, approval_id: str, consent_id: str):
        approval = self.receipt("approvals", approval_id); consent = self.receipt("consents", consent_id)
        if approval["policy_sha256"] != self.policy_sha or approval["policy_document_sha256"] != self.policy_doc_sha:
            raise HTTPException(403, "stale policy approval")
        if consent["consent_sha256"] != self.consent_sha or consent["approval_receipt_id"] != approval_id:
            raise HTTPException(403, "stale consent")
        return approval, consent

    def expire_recordings(self):
        current = now()
        for directory in sorted((self.root / "recordings").iterdir()):
            record_path = directory / "record.json"
            if not directory.is_dir() or not record_path.exists():
                continue
            record = json.loads(record_path.read_text())
            deadline = datetime.fromisoformat(record["retention"]["raw_delete_after"])
            if deadline > current:
                continue
            tombstone = {
                "fixture_id": record["fixture_id"],
                "requested_at": current.isoformat(),
                "reason": "90-day raw retention expired",
                "trained_model_influence_reversed": False,
            }
            (self.root / "governance" / "deletion-requests" / f"{record['fixture_id']}.json").write_text(json.dumps(tombstone, indent=2) + "\n")
            shutil.rmtree(directory)

    def records(self) -> list[dict]:
        records=[]
        for path in sorted((self.root / "recordings").glob("*/record.json")):
            record=json.loads(path.read_text()); _validate_records([record], "audio-fixture-manifest.v2.schema.json"); records.append(record)
        return records

    def rebuild_manifest(self):
        records=self.records(); target=self.root/"manifests/founder-chennai-v1.jsonl"; tmp=target.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r,ensure_ascii=False,sort_keys=True)+"\n" for r in records)); tmp.replace(target)

    def accepted_prompt_ids(self) -> set[str]: return {r["prompt"]["prompt_id"] for r in self.records()}

    def skipped_prompt_ids(self) -> set[str]:
        return {path.stem for path in (self.root / "studio" / "skips").glob("*.json")}

    async def skip(self, prompt_id: str, request: SkipRequest):
        if prompt_id not in self.prompts:
            raise HTTPException(404, "unknown prompt")
        if prompt_id in self.accepted_prompt_ids():
            raise HTTPException(409, "prompt already accepted")
        payload = {"prompt_id": prompt_id, "reason": request.reason[:200], "skipped_at": now().isoformat()}
        path = self.root / "studio" / "skips" / f"{prompt_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(path)
        return payload

    async def approve(self, request: ApprovalRequest):
        if request.role != "founder" or not request.approver_name.strip() or not all([request.allow_evaluation,request.allow_named_providers,request.allow_model_training,request.prohibit_voice_cloning,request.prohibit_real_person_data,request.accept_90_day_retention]):
            raise HTTPException(400,"all founder policy approvals are required")
        payload={"receipt_id":f"approval-{uuid.uuid4().hex[:16]}","policy_id":self.policy["policy_id"],"policy_version":1,"policy_sha256":self.policy_sha,"policy_document_sha256":self.policy_doc_sha,"approver_name":request.approver_name.strip(),"role":"founder","approved_at":now().isoformat(),"approved_uses":self.policy["approved_uses"],"named_providers":self.policy["named_providers"],"retention_days":90,"voice_cloning_allowed":False}
        path=self.root/"governance/approvals"/f"{payload['receipt_id']}.json"; path.write_text(json.dumps(payload,indent=2)+"\n"); return payload

    async def consent(self, request: ConsentRequest):
        approval=self.latest_receipt("approvals")
        if not approval: raise HTTPException(403,"founder approval required")
        if request.participant_name.strip().casefold() != approval["approver_name"].casefold() or not all([request.adult_self_recording,request.alone_no_background_speakers,request.fictional_prompts_only,request.no_real_person_data,request.allow_evaluation,request.allow_named_providers,request.allow_model_training,request.understand_deletion_limit]): raise HTTPException(400,"all participant consent statements are required")
        payload={"receipt_id":f"consent-{uuid.uuid4().hex[:16]}","participant_id":"founder-001","participant_name":request.participant_name.strip(),"approval_receipt_id":approval["receipt_id"],"consent_version":1,"consent_sha256":self.consent_sha,"consented_at":now().isoformat(),"approved_uses":approval["approved_uses"],"named_providers":approval["named_providers"],"understands_trained_model_deletion_limit":True}
        path=self.root/"governance/consents"/f"{payload['receipt_id']}.json"; path.write_text(json.dumps(payload,indent=2)+"\n"); return payload

    async def accept(self, request: RecordingRequest):
        prompt=self.prompts.get(request.prompt_id)
        if not prompt: raise HTTPException(404,"unknown prompt")
        approval,consent=self.validate_governance(request.approval_receipt_id,request.consent_receipt_id)
        if request.transcript.strip()!=prompt["text"]: raise HTTPException(400,"transcript must match the assigned fictional prompt")
        try: audio=base64.b64decode(request.audio_base64,validate=True)
        except Exception as exc: raise HTTPException(400,"invalid audio") from exc
        if not 16000<=len(audio)<=2_000_000: raise HTTPException(400,"audio size outside bounds")
        async with self.lock:
            existing=[r for r in self.records() if r["capture"]["request_id"]==request.request_id]
            if existing:
                if existing[0]["prompt"]["prompt_id"]!=request.prompt_id or existing[0]["audio"]["sha256"]!=hashlib.sha256(audio).hexdigest(): raise HTTPException(409,"request ID reused with different content")
                return existing[0]
            if request.prompt_id in self.accepted_prompt_ids(): raise HTTPException(409,"prompt already accepted")
            fixture_id=f"VF-FOUNDER-{uuid.uuid4().hex[:12].upper()}"; stage=self.root/".staging"/fixture_id; stage.mkdir()
            wav=stage/"audio.wav"; wav.write_bytes(audio); meta=inspect_wav(wav)
            if meta.sample_rate_hz!=16000 or not 500<=meta.duration_ms<=30000: shutil.rmtree(stage); raise HTTPException(400,"canonical 16 kHz WAV with 0.5-30s duration required")
            with wave.open(str(wav), "rb") as source:
                values = array("h"); values.frombytes(source.readframes(source.getnframes()))
            if __import__("sys").byteorder != "little": values.byteswap()
            total = len(values); peak = max((abs(value) for value in values), default=0) / 32768
            clipped_ratio = sum(abs(value) >= 32735 for value in values) / total if total else 0
            speech_ratio = sum(abs(value) >= 492 for value in values) / total if total else 0
            if speech_ratio < 0.03: shutil.rmtree(stage); raise HTTPException(400,"server detected almost no speech")
            if clipped_ratio > 0.10: shutil.rmtree(stage); raise HTTPException(400,"server detected severe clipping")
            if abs(peak-request.peak)>0.10 or abs(clipped_ratio-request.clipped_ratio)>0.02: shutil.rmtree(stage); raise HTTPException(400,"client/server quality metrics disagree")
            recorded=now(); record={"schema_version":2,"fixture_id":fixture_id,"audio":{"relative_path":f"recordings/{fixture_id}/audio.wav","sha256":meta.sha256,"media_type":"audio/wav","sample_rate_hz":16000,"channels":1,"sample_width_bits":16,"duration_ms":meta.duration_ms},"locale":prompt["locale"],"speech_style":prompt["speech_style"],"condition":"browser_call","split":"development","speaker_id":"founder-001","reference":{"prompt_text":prompt["text"],"transcript":request.transcript,"intent_label":prompt["intent"],"critical_entities":prompt["critical_entities"]},"provenance":{"source":"human_recording","participant_id":"founder-001","approved_uses":approval["approved_uses"],"named_providers":approval["named_providers"],"voice_cloning_allowed":False,"minor_participant":False,"background_speakers_present":False,"contains_real_patient_data":False,"redistribution_allowed":False,"access_roles":["founder","authorized_voice_rnd_reviewer"]},"annotation":{"language_review_status":"self_recorded_unreviewed","domain_review_status":"unreviewed","annotation_status":"draft","annotator_roles":["founder"],"revision":1},"capture":{"software_version":"founder-studio-v1","source_sample_rate_hz":request.source_sample_rate_hz,"canonical_sample_rate_hz":16000,"attempt":request.attempt,"request_id":request.request_id,"profile":"browser_call","peak":request.peak,"clipped_ratio":request.clipped_ratio,"speech_ratio":request.speech_ratio,"leading_silence_ms":request.leading_silence_ms,"trailing_silence_ms":request.trailing_silence_ms},"governance":{"policy_id":self.policy["policy_id"],"policy_version":1,"policy_sha256":self.policy_sha,"approval_receipt_id":approval["receipt_id"],"consent_version":1,"consent_sha256":self.consent_sha,"consent_receipt_id":consent["receipt_id"]},"prompt":{"pack_id":self.pack["pack_id"],"pack_version":1,"prompt_id":request.prompt_id},"retention":{"recorded_at":recorded.isoformat(),"raw_delete_after":(recorded+timedelta(days=90)).isoformat(),"deletion_status":"active","trained_model_influence_reversible":False}}
            _validate_records([record],"audio-fixture-manifest.v2.schema.json"); (stage/"record.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n"); stage.replace(self.root/"recordings"/fixture_id); self.rebuild_manifest(); return record

    async def delete(self, fixture_id: str, request: DeleteRequest):
        if request.confirm_fixture_id!=fixture_id: raise HTTPException(400,"fixture confirmation mismatch")
        async with self.lock:
            source=self.root/"recordings"/fixture_id
            if not source.exists(): raise HTTPException(404)
            trash=self.root/".trash"/fixture_id; source.replace(trash)
            tombstone={"fixture_id":fixture_id,"requested_at":now().isoformat(),"reason":request.reason,"trained_model_influence_reversed":False}
            (self.root/"governance/deletion-requests"/f"{fixture_id}.json").write_text(json.dumps(tombstone,indent=2)+"\n")
            shutil.rmtree(trash); self.rebuild_manifest(); return tombstone
