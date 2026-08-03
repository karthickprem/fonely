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
