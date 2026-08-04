# Startup and Prewarm Research

## Verified source facts

OpenAI places every startup step on the critical path. Its WARP and Instant Connect work reduces media/data startup and removes SDP signaling from the critical path through transport capabilities not currently present in Fonely's stack.

OpenAI also pre-creates and prefills a frontier-model session, keeps session affinity, and uses prompt caching to reduce delegation latency.

## Fonely inference

Fonely should first optimize application-controlled startup before considering transport rewrites:

- measure one end-to-end click-to-first-audible timeline;
- initialize independent resources concurrently;
- preload safe local VAD/Smart Turn/style assets;
- cache fixed greeting audio for later evaluation;
- keep provider state session-scoped unless pooling is explicitly supported;
- never create hidden billable traffic for warming.

## Milestones

```text
session_requested
bot_factory_entered
transport_created
local_models_ready
providers_constructed
pipeline_started
webrtc_connected
greeting_queued
tts_first_audio
bot_started_speaking
browser_playback_started
```

## Experimental arms

- cold;
- local warm;
- provider-safe construction/connect only.

## Adoption rule

A prewarm change is accepted only when it improves a named median or p95 milestone by at least 10%, causes no provider charge/privacy uncertainty, and produces no session leakage or startup failures.

## Rejected or deferred approaches

- Hand-implementing WARP in this POC: deferred; requires transport support and separate protocol work.
- Rewriting Pipecat in Go: rejected without measured Python scheduling bottleneck.
- Hidden provider inference to warm caches: prohibited without cost/privacy authorization.
- Global mutable provider/conversation state: rejected because it risks tenant/session leakage.
