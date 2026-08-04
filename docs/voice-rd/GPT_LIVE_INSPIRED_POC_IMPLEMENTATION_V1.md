# GPT-Live-Inspired POC Implementation V1

Status: implemented and unit/integration tested on `dev4/voice-rd-lab`; not committed at time of this record.

## Boundary

This is a Pipecat cascade lifecycle POC, not GPT-Live, native speech reasoning, WARP, or production integration.

## Changed components

- `voice-lab/live_poc.py`: session-local turn/generation and delegate lifecycle.
- `voice-lab/delegation.py`: Pipecat generation/delegation processor and output boundary.
- `voice-lab/pipeline.py`: coordinator inserted after user aggregation; Cartesia and deterministic safety preserved.
- `voice-lab/voice_eval/observer.py`: sanitized POC event sink.
- `voice-lab/voice_eval/live_poc_runner.py`: provider-free logical-time concurrency evidence.
- realtime POC schemas and frozen scenario/config files.
- browser client: accurate Cartesia controls and independent mic/playback/delegate/transcript/generation states.

## Implemented guarantees

- one coordinator per session;
- monotonic generation validity;
- read-only synthetic delayed lookup only;
- cooperative cancellation and late-result stale suppression;
- speculative/final/authoritative transcript types remain distinct;
- operational telemetry stores lengths/status, not transcript or payload text;
- existing WebRTC media track remains the only audio path;
- deterministic safety remains before Claude;
- no production/backend mutation surface exists.

## Known implementation limits

- Current Saaras path does not provide reliable live partial transcripts, so speculative-view behavior is contract/simulation evidence rather than provider proof.
- Pipecat interruption clears queued output; the output gate does not yet receive provider-native generation tags on each TTS frame.
- Browser `onServerMessage` support was implemented against the Pipecat client callback contract but requires real-browser observation per build.
- Startup evidence currently records server milestones; browser first-audible milestone requires a later client-to-server evidence channel.
- Local prewarm is designed but not adopted pending repeated cold/warm measurements.
