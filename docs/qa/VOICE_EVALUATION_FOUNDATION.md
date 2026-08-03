# Voice Evaluation Foundation V1

**Status:** Implemented evaluation tooling; synthetic evidence only.

The foundation keeps conversation oracles (`evals/`) separate from external audio manifests and run evidence. Runtime data must live under a required `VOICE_EVAL_DATA_ROOT` outside Git.

## Commands

```bash
python voice-lab/voice_eval/cli.py validate --manifest "$VOICE_EVAL_DATA_ROOT/manifests/foundation-v1.jsonl" --data-root "$VOICE_EVAL_DATA_ROOT"
python voice-lab/voice_eval/cli.py run-saaras --manifest "$VOICE_EVAL_DATA_ROOT/manifests/foundation-v1.jsonl" --data-root "$VOICE_EVAL_DATA_ROOT" --modes transcribe,codemix --run-id VR-foundation-v1 --output "$VOICE_EVAL_DATA_ROOT/runs/VR-foundation-v1.jsonl"
python voice-lab/voice_eval/cli.py report --manifest "$VOICE_EVAL_DATA_ROOT/manifests/foundation-v1.jsonl" --data-root "$VOICE_EVAL_DATA_ROOT" --results "$VOICE_EVAL_DATA_ROOT/runs/VR-foundation-v1.jsonl" --report-id foundation-v1 --output "$VOICE_EVAL_DATA_ROOT/reports/foundation-v1.json"
python voice-lab/voice_eval/cli.py serve --host 127.0.0.1 --port 3010 --data-root "$VOICE_EVAL_DATA_ROOT"
```

The UI is loopback-only and collection defaults to disabled. On startup it prints a URL containing a one-time random token in the URL fragment; use that exact URL. API calls require the token, enforce loopback Host/Origin, and never place it in query strings. Real-speaker collection remains blocked pending approval of `VOICE_RD_CONSENT_TEMPLATE.v1.md`.

Reports are evidence-only and do not approve a provider, language, pilot or production claim.

## Founder recording studio

Start the self-recording studio:

```bash
python voice-lab/voice_eval/cli.py serve \
  --host 127.0.0.1 --port 3049 \
  --data-root /scratch/karthick/fonely-founder-recording-data \
  --collection-mode founder_recording
```

From a different computer, create a local tunnel:

```bash
ssh -N -L 3049:127.0.0.1:3049 xhdctallapa40
```

Open the exact `http://127.0.0.1:3049/#token=...` URL printed by the server. The fragment token is required and removed from browser history after loading.

The studio records one of 50 fictional prompts at a time, captures source-rate Float32 audio, resamples it to canonical 16 kHz mono PCM16 WAV, shows waveform/peak/clipping/silence diagnostics, allows replay/re-record, and uploads only after explicit Accept. Progress is reconstructed from committed external recordings, not browser local storage.

Founder mode requires a current founder policy approval and participant consent receipt. It permits only Karthick's self-recordings for evaluation, the named providers in the policy, and internal model training. It prohibits real patient/customer data, background speakers, minors, voice cloning and redistribution. Raw retention is 90 days; deletion cannot guarantee removal of influence from already-completed model training.

Fifty founder clips are bootstrap development/model-training data from one speaker, not representative Chennai validation or a speaker-disjoint test set.
