"""Live run preparation: commands, credential validation, recording paths.

Everything needed to start a live browser STT→LLM→TTS conversation
the moment credentials arrive.  No provider values in source.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class CredentialStatus:
    name: str
    status: str  # SET, UNSET, SET_SHORT
    min_length: int = 8


REQUIRED_CREDENTIALS = [
    CredentialStatus("SARVAM_API_KEY", "UNSET", 20),
    CredentialStatus("ANTHROPIC_API_KEY", "UNSET", 20),
    CredentialStatus("CARTESIA_API_KEY", "UNSET", 20),
    CredentialStatus("CARTESIA_VOICE_ID", "UNSET", 8),
]


def validate_credentials() -> tuple[bool, list[dict[str, str]]]:
    """Validate all required credentials exist without logging values.

    Returns (all_ready, [{name, status, meets_minimum}]).
    """
    results = []
    all_ready = True
    for cred in REQUIRED_CREDENTIALS:
        value = os.environ.get(cred.name, "")
        if not value:
            status = "UNSET"
            meets_min = False
        elif len(value) < cred.min_length:
            status = "SET_SHORT"
            meets_min = False
        else:
            status = "SET"
            meets_min = True
        results.append({
            "name": cred.name,
            "status": status,
            "meets_minimum_length": str(meets_min),
        })
        if not meets_min:
            all_ready = False
    return all_ready, results


@dataclass(frozen=True)
class RecordingPaths:
    """Sanitized artifact paths for live conversation recording."""
    base_dir: str
    session_id: str
    transcript_path: str = ""
    evidence_path: str = ""
    telemetry_path: str = ""
    native_review_path: str = ""

    @classmethod
    def for_session(cls, session_id: str, base: str = "/tmp/fonely-voice-evidence") -> RecordingPaths:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base_dir = f"{base}/{ts}-{session_id}"
        return cls(
            base_dir=base_dir,
            session_id=session_id,
            transcript_path=f"{base_dir}/transcript.json",
            evidence_path=f"{base_dir}/evidence.json",
            telemetry_path=f"{base_dir}/telemetry.jsonl",
            native_review_path=f"{base_dir}/native-review.json",
        )

    def ensure_dirs(self) -> None:
        Path(self.base_dir).mkdir(parents=True, exist_ok=True)


LIVE_RUN_CHECKLIST = """
FONELY LIVE VOICE CONVERSATION — PRE-RUN CHECKLIST
====================================================

1. CREDENTIALS (run: python -c "from fonely.voice.runbook import validate_credentials; print(validate_credentials())")
   - SARVAM_API_KEY:    must be SET (>=20 chars)
   - ANTHROPIC_API_KEY: must be SET (>=20 chars)
   - CARTESIA_API_KEY:  must be SET (>=20 chars)
   - CARTESIA_VOICE_ID: must be SET (>=8 chars, Kavitha voice ID)

2. NETWORK
   - Verify outbound HTTPS to api.sarvam.ai, api.anthropic.com, api.cartesia.ai
   - Verify port 3000 available for WebRTC signaling

3. BROWSER
   - Chrome/Edge with microphone permission
   - Navigate to http://localhost:3000/voice-lab after server start

4. START SERVER (from voice-lab directory):
   cd /scratch/karthick/fonely-worktrees/dev4-cartesia/voice-lab
   source /scratch/karthick/fonely/backend/.venv/bin/activate
   export SARVAM_API_KEY=<value>
   export ANTHROPIC_API_KEY=<value>
   export CARTESIA_API_KEY=<value>
   export CARTESIA_VOICE_ID=<value>
   python -m pipecat.runner.run --host 0.0.0.0 --port 3000

5. RECORDING
   - Conversation auto-records if VOICE_EVAL_DATA_ROOT is set
   - Set: export VOICE_EVAL_DATA_ROOT=/tmp/fonely-voice-evidence
   - Telemetry writes to $VOICE_EVAL_DATA_ROOT/telemetry/<session_id>.jsonl

6. TEST SCENARIO (AC-002: today availability)
   - Say: "இன்னைக்கு doctor free-ஆ?"
   - Expected: Agent resolves today, queries typed availability, reports actual slots
   - NOT expected: "எந்த day-ன்னு தெரியல" or generic Mon-Sat hours

7. POST-RUN
   - Save transcript from browser
   - Save telemetry JSONL
   - Generate native review worksheet
   - Record latency: user-stop → first LLM token → first TTS audio

8. EVIDENCE ARTIFACTS
   - transcript.json: sanitized turn-by-turn (no raw audio)
   - evidence.json: latency, usage, cost, terminal outcome, turn count
   - telemetry.jsonl: provider usage events
   - native-review.json: naturalness checks + native speaker ratings

BOUNDARIES:
   - Rejected validator remains FailClosedValidatorStub
   - No consequential speech (booking confirmation, notification, handoff) reaches TTS
   - No authoritative business mutations
   - No merge/deploy until acceptance gates pass
"""


NATIVE_REVIEW_PROCEDURE = """
NATIVE TAMIL/TANGLISH REVIEW PROCEDURE
========================================

Reviewer: Native Tamil speaker familiar with Chennai Tanglish

For each recorded conversation turn:

1. NATURALNESS (1-5 scale):
   1 = Unnatural/robotic
   2 = Understandable but awkward
   3 = Acceptable but formal
   4 = Natural conversational
   5 = Indistinguishable from human receptionist

2. CODE-SWITCHING:
   - Tamil script for Tamil words? (yes/no)
   - English dental/appointment terms in English? (yes/no)
   - Awkward script mixing? (yes/no)

3. PRONUNCIATION (from recorded audio):
   - Doctor names pronounced correctly? (yes/no)
   - Tamil numbers/times natural? (yes/no)
   - Isolated suffix ஆ/AA detected? (yes/no)
   - First-word audio breakage? (yes/no)

4. RESPONSE QUALITY:
   - Answered the question directly? (yes/no)
   - Unnecessary filler/narration? (yes/no)
   - Multiple questions in one response? (yes/no)
   - Repeated information? (yes/no)

5. NOTES: Free-form comments on each turn.

Submit completed worksheet as native-review.json.
"""
