# GPT-Live-Inspired Fonely POC Protocol

Freeze ID: `GPT-LIVE-INSPIRED-POC-V1`
Status: research-only, pre-registered before implementation.

## Hypotheses

1. The existing SmallWebRTC cascade can keep microphone/VAD/STT processing active during Cartesia playback and a delayed read-only delegate.
2. Generation tokens can prevent stale delegate/response results from reaching output or authority after interruption.
3. Speculative and final transcript views can remain separate from deterministic authoritative state.
4. Safe local prewarm can improve at least one named startup milestone by 10% without provider spend or cross-session leakage.
5. Session-local coordination remains isolated and leak-free in a deterministic 25-session × 100-turn simulation.

## Fixed cascade

```text
SmallWebRTC
→ Saaras v3 codemix STT
→ user aggregator
→ generation/delegation coordinator
→ deterministic safety
→ Chennai style
→ fixed Claude model/prompt
→ Cartesia Sonic 3.5 Kavitha
→ generation output gate
→ SmallWebRTC output
```

Only orchestration and observability may change. Providers and deterministic authority remain fixed.

## Scope

- synthetic clinic facts and read-only delayed lookup only;
- no production backend, booking command, customer/patient data, provider purchase, account creation, or deployment;
- no GPT-Live/native speech provider;
- no WARP implementation or equivalence claim.

## Transcript and authority policy

- speculative transcript: replaceable UI/preparation evidence only;
- final transcript: immutable raw provider evidence;
- authoritative value: deterministic validation, canonical readback, and explicit confirmation only;
- final STT is not automatically authoritative.

## Frozen delayed-lookup scenarios

- completes before interruption;
- cooperative cancellation;
- cancellation-resistant late completion;
- timeout;
- deterministic failure;
- disconnect while pending;
- next generation starts before prior completion;
- identical turn numbers in independent sessions.

## Metrics

- startup milestone offsets;
- microphone events during playback/delegation;
- generation advances;
- delegate started/completed/cancelled/timed out/stale-dropped;
- stale text/audio/output suppression;
- interruption-to-old-output-stop;
- pending tasks/queue depth and cleanup;
- concurrent-session isolation;
- RTT, jitter, packet loss and applied browser audio constraints where observable.

## Acceptance gates

- 100% microphone continuity during playback and delegate delay;
- 100% stale-result suppression;
- zero stale text/audio entering output or assistant history;
- zero unauthorized mutation attempts;
- zero cross-session events over 2,500 deterministic turns;
- zero leaked session tasks after cleanup;
- identical outcomes across three frozen simulation runs;
- p95 interruption-to-old-output-stop ≤350 ms on the reference local setup;
- schema-valid immutable evidence;
- no transcript/audio/credential leakage in operational telemetry.

## Startup experiment

Compare:

- `cold`: current initialization;
- `local_warm`: preload safe local VAD/Smart Turn/style assets;
- `provider_safe`: only documented non-billable construction/connect behavior.

Run at least 10 cold and 20 warm starts on the same host/browser. Hidden billable inference is forbidden. Unknown billing or privacy behavior blocks provider prewarm.

## Evidence

- machine schemas/results under `voice-lab/voice_eval/`;
- sensitive raw evaluation evidence under external `VOICE_EVAL_DATA_ROOT`;
- repository summaries contain hashes/configuration, not recordings or PII;
- existing immutable writers refuse replacement.

## Stop conditions

Stop if implementation requires provider changes, production mutation, write-capable delegation, credentials/spend, dependency upgrade, raw sensitive telemetry, deterministic-authority bypass, WARP/native-speech claims, cross-session leakage, task leaks, or stale output reaching users.

Passing this POC establishes only local R&D evidence. Production integration remains separately authorized and gated.
