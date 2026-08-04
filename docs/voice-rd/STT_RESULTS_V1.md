# STT Results V1

## Evidence available

Only the frozen Saaras V0 arms have real consented founder-audio results. Challenger execution is blocked by access; it is not classified as failed.

### Corpus

- 50 consented founder recordings;
- 25 Tamil and 25 Tanglish;
- 16 kHz browser audio;
- one speaker;
- development split only;
- annotations remain draft;
- no simulated 8 kHz, blind holdout, or real PSTN evidence.

### Raw metrics

| Arm | Successful | Failed | Raw macro WER | Reported p50 | Reported p95 | Semantic critical exactness | Gate |
|---|---:|---:|---:|---:|---:|---:|---|
| Saaras v3 `transcribe` | 49 | 1 | 71.85% | 313 ms | 648 ms | 76.0% | Failed |
| Saaras v3 `codemix` | 50 | 0 | 49.99% | 318 ms | 617 ms | 82.4% | Failed |

Timing caveat: historical `wall_ms` may be replaced by provider processing latency. It is not established first-final or end-of-speech-to-final latency.

### Native live findings

- `பல்லு வலிங்க` was recognized as `கல்லு வலிங்க`;
- Tamil speech appeared in Telugu script;
- duplicated/fragmented turns occurred;
- names, symptoms, phone numbers, dates, and times were unreliable.

### V0 classification

```text
Understanding:          FAILED
Critical entities:      FAILED
Tamil script stability: FAILED
Pilot eligibility:      FAILED
```

## Challenger status

| Challenger | Direct access | Results |
|---|---|---|
| OpenAI `gpt-live-transcribe` | Blocked: key unavailable | None |
| Google `chirp_2` `ta-IN` | Blocked: project/credentials unavailable | None |
| Azure stable Tamil path | Blocked: key/region unavailable | None |
| Speechmatics | Blocked: key unavailable and Tamil support not promoted | None |
| AI4Bharat | Offline runtime/license review incomplete | None |

No customer/patient data was used. No provider account was created. No money was spent.
