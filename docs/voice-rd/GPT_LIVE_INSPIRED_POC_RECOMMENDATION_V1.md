# GPT-Live-Inspired POC Recommendation V1

## Adopt experimentally

- Keep SmallWebRTC media independent from read-only asynchronous work.
- Use session/turn/generation tokens for every asynchronous result.
- Treat cancellation as best effort and stale suppression as mandatory.
- Preserve speculative, final-evidence, and deterministic-authoritative states separately.
- Expand startup and lifecycle telemetry before optimizing providers.
- Keep Cartesia Kavitha as the current TTS baseline.

## Do not adopt yet

- Native GPT-Live architecture or provider dependency;
- removal of VAD/Smart Turn from the current cascade;
- WARP implementation;
- Python-to-Go rewrite;
- provider connection pooling or hidden warming;
- write-capable asynchronous delegation;
- any production backend integration.

## Next gate

1. Correct and re-run the live POC with trusted Claude gateway headers from the first request.
2. Verify delegate start/finish/stale events in actual browser telemetry.
3. Measure interruption to last audible old output, not callback proxies.
4. Complete cold/local-warm startup samples.
5. Run bounded real SmallWebRTC multi-peer soak with approved provider cost limits.
6. Obtain independent review before any integration decision.

Production integration remains unauthorized.
