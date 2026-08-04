# STT Experiment Protocol

## Scope

Research-only comparison. It does not authorize production integration, provider purchases, customer data, real patient calls, deployment, or merge to `main`.

## Fixed cascade

```text
VAD / turn manager
→ provider-neutral STT evidence
→ deterministic critical-field state
→ application commands (not part of this experiment)
→ fixed Claude realization
→ Cartesia Kavitha
```

An STT arm changes STT only. Claude model, system instructions, safety/style processors, Cartesia voice, audio fixtures, and scoring stay fixed.

## Frozen incumbent

`voice-lab/voice_eval/frozen/saaras-v3-v0.json` freezes:

- Saaras v3 `transcribe`;
- Saaras v3 `codemix`;
- source commit and dependency versions;
- fixture manifest and result paths;
- known failures and measurement caveats.

A changed configuration requires a new freeze ID and cannot overwrite V0.

## Corpus stages

1. Development: tuning of documented provider controls.
2. Validation: configuration selection.
3. Blind holdout: no provider controls, thresholds, normalization, turn rules, or corpus changes.
4. Deterministically simulated 8 kHz: separate report and hashes.
5. Real PSTN: prohibited until separately authorized and consented.

All human splits are speaker-disjoint. Unreadable/provider-failed fixtures remain result rows.

## Evidence preservation

- Raw provider output is immutable.
- Normalized candidates are separate fields.
- Wrong-script rate is computed on raw output.
- No manual transcript correction before scoring.
- Every result carries fixture hash and frozen arm ID.
- Writers fail when an evidence target already exists.
- Retries are new evidence files linked to the original; they never replace failures.

## Metrics

Per fixture:

- WER and CER where meaningful;
- semantic intent;
- per-critical-entity exactness;
- raw wrong-script result;
- partial/final sequence;
- duplicate-final and fragmented-turn evidence;
- first partial, first final, and end-of-speech-to-final latency;
- endpoint result, reconnects, errors, usage, and cost evidence.

Aggregate reports do not average away malformed or unconfirmed critical values.

## Confidence

Raw provider confidence is preserved but never compared directly between providers. Calibration is provider-specific and requires development-corpus error buckets. Absence of confidence is recorded as unknown, never invented.

## Promotion gates

- phone exactness ≥99%;
- malformed authoritative values accepted = 0;
- unconfirmed authoritative values used = 0;
- cross-attempt digit concatenation = 0;
- critical intent/entity exactness ≥98%;
- name exact-or-confirmed ≥98%;
- date/time exact-or-confirmed ≥99%;
- wrong-script final rate <0.5%;
- duplicate-final rate <0.5%;
- fragmented-turn failure <1%;
- false endpoint <2%;
- false booking confirmation = 0;
- browser 16 kHz first-final p95 ≤1.0 s;
- simulated 8 kHz first-final p95 ≤1.5 s.

## Access gate

Before adapter work, record official Tamil/live support, credential/account/region needs, expected spend, retention/training defaults, and legal blockers. A blocked provider is not a failed provider.

As of 2026-08-04, only Sarvam is accessible. OpenAI, Google, Azure, and Speechmatics are access-blocked by missing credentials. No spending or account creation occurred.
