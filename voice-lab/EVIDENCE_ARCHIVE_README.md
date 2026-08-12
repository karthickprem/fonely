# Voice-lab experimental evidence archive

Archival snapshot of accepted experimental proof artifacts from the Dev4 voice
R&D lab. **This is archival, not product integration.** None of these files is
product code, none is merged into the shipping backend, and this commit is a
local provenance record only — not pushed, not a release.

## Scope of this archive (strict)

Included, and only these:

| File | What it is |
|---|---|
| `cost_meter.py` | Per-turn cost meter for the voice pipeline — raw measured provider units (STT audio-seconds, LLM prompt/completion tokens kept separate, TTS characters). Correlates by conversation_id + business_id only; never PII/transcript/audio in a record. Money only from a dated, sourced PricingBook. |
| `cost_meter_dryrun.py` | Zero-provider offline dry run: replays the captured usage fixture through the meter with a dated PricingBook. No provider calls, no credentials — reads the two JSON fixtures only. Proves idempotent replay, reconciliation, retry separation, estimate math, estimated-vs-invoiced separation. |
| `cost_meter_proof.py` | Cost-meter proof driven against live provider usage. Reads named env keys at runtime (see Security). |
| `stt_audio_proof.py` | STT-on-audio proof: generates each booking turn as Tamil audio (Cartesia TTS), transcribes with real Sarvam STT (saaras:v3), feeds the REAL STT output through the production FrameProcessors, asserts the committed PG row. Reports clean-input vs STT-output per turn so mis-recognitions are visible. Reads named env keys at runtime. |
| `tenant_concurrency_proof.py` | Correct-tenant-under-concurrency proof, in-process against fonely_dev4. Fires concurrent bookings for two different tenants through the production CommandPath, asserts each row lands under its own business_id, and adversarially confirms a session bound to tenant A cannot commit under tenant B. Cleans up its own rows. |
| `fixtures/captured_usage_costmeter-*.json` | A captured raw-usage sequence (turn index, provider, model, unit_kind, unit_count, attempt) used by the dry run. Token/second COUNTS only. |
| `fixtures/pricing_internal_test_v1.json` | Internal-test placeholder pricing book. Explicitly NOT a provider quote or contracted price — for exercising the estimate math only. |

**Excluded** as unrelated / unaccepted lab runtime: `clinic_context.py` and all
`test_*.py` live conversation harnesses (`test_50_conversations.py`,
`test_live_talk.py`, `test_prehandoff.py`, `test_real_conversations.py`).

## Security boundaries (verified before archiving)

- **No secret values.** The scripts that talk to live providers
  (`stt_audio_proof.py`, `cost_meter_proof.py`) read `/scratch/karthick/fonely/.env`
  at runtime and reference keys **by name** (e.g. `_ENV["CARTESIA_API_KEY"]`,
  `_ENV["SARVAM_API_KEY"]`). No key value is embedded in any archived file. The
  `.env` itself is NOT in this archive.
- **No PII.** Phone numbers in the proofs are synthetic test values
  (`+9198400000xx`). No real caller data, transcript text, or audio bytes.
- **Fixtures are value-clean.** The captured-usage JSON holds only unit counts
  and model names; the pricing JSON is a marked internal-test placeholder.

## Evidence scopes and what is NOT proven

Each proof is scoped to exactly what its assertions show:

- The cost-meter dry run is fully offline and reproducible.
- `stt_audio_proof.py` and `cost_meter_proof.py` require live provider
  credentials (via `.env`) and network reachability; they are **NOT RUN** as
  part of this archive and their last-run results are not asserted here.
- `tenant_concurrency_proof.py` runs in-process against a local `fonely_dev4`;
  it never touches an external service or real credentials.

**NOT proven by anything here:** a real telephone/WebRTC call, native-speaker
review of the Tamil, or production deployability of any provider gateway. Those
remain founder gates.

## Runtime dependencies (archival note)

Some scripts import sibling lab modules not included in this strict archive
(e.g. `stt_audio_proof.py` imports `live_proof`; `cost_meter_proof.py` /
`cost_meter_dryrun.py` import `cost_meter`). The archive preserves the accepted
evidence artifacts as-authored; it does not repackage them to run standalone.
