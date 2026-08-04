# STT Recommendation V1

## Experimental recommendation

There is **no winning STT provider yet**.

- Preserve Saaras v3 `transcribe` and `codemix` as immutable failed V0 baselines.
- Keep Cartesia Kavitha as the TTS baseline; its selection does not imply an STT decision.
- Highest-priority challenger remains OpenAI `gpt-live-transcribe`, but Tamil support, access, quality, latency, and terms are unproven.
- Google `chirp_2` `ta-IN` is the strongest documentation-proven Tamil feature matrix, but live streaming for that exact region/model/locale is not established.
- Azure remains required, but the stable live Tamil path is not yet proven.
- Speechmatics and AI4Bharat remain exploratory.
- Deepgram Flux/Nova-3 Tamil adapter work remains excluded without new official evidence.

## Access decision

As of 2026-08-04:

- Sarvam credential: available;
- OpenAI credential: unavailable;
- Google project/credential: unavailable;
- Azure credential/region: unavailable;
- Speechmatics credential: unavailable.

The assignment prohibits account creation, purchases, or accepting provider terms. Therefore the minimum “two real-audio challengers” execution gate is blocked pending founder-approved access and privacy/terms review.

## Required next gate

For each accessible challenger:

1. one non-sensitive access probe;
2. same frozen founder fixtures;
3. separate native 16 kHz and deterministic simulated 8 kHz results;
4. raw script-leakage measurement;
5. critical-field and 100-case phone evaluation;
6. provider-specific confidence calibration;
7. blinded native-listener review;
8. total cost per independently verified successful booking;
9. frozen blind holdout.

## Production status

Production integration is **not authorized** and is not recommended from current evidence. Real telephony, licensing, privacy, reliability, backend integration, staging, and independent cofounder review remain separate gates.
