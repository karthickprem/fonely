# OpenAI Realtime System Research

Status: primary-source research record, accessed 2026-08-04.

## Source identity

- Title: **How we built a realtime system for responsive voice AI in six months**
- Authors: Justin Uberti and Zahan Malkani, OpenAI Members of Technical Staff
- Canonical URL: https://openai.com/index/continuous-voice-interaction-with-gpt-live/
- Source supplied in full by Karthick after automated retrieval returned HTTP 403.

This document separates article facts, OpenAI-specific choices, Fonely inferences, and POC hypotheses. It supersedes the unresolved-source status in `OPENAI_RESEARCH_APPLICABILITY.md`; that earlier document remains as research-history evidence.

## Primary-source facts

### Full-duplex model controls conversational timing

GPT-Live is a full-duplex native voice model: it listens and speaks simultaneously. OpenAI removed a separate turn detector from its audio path and streams audio continuously into and out of the voice model.

### Dedicated media path

OpenAI separates media from application and business logic. Audio travels on a small, predictable fast path. Delegation, tools, persistence, and application work run behind an asynchronous RPC boundary, so a slow tool delays its result but does not stall media.

### Continuous stateful inference

The media frontend must deliver frames on schedule. OpenAI moved its media frontend and inference logic from Python asyncio to Go; the reported new p95 frame delivery matched the previous p50. WebRTC handles low-latency media, packet loss, clock drift, and client changes.

### Seamless instance handoff and compaction

For long sessions, OpenAI warms a replacement model instance alongside the active one, prefills it with session context, temporarily runs both, and cuts over when ready. Context compaction is performed as a managed transition so KV-cache rebuilding does not interrupt media.

### Asynchronous delegation

GPT-Live keeps conversational continuity while a frontier model performs deeper reasoning, search, or tool use. OpenAI pre-creates and prefills the frontier-model session at voice-session startup, keeps session affinity, uses prompt caching, and tunes reasoning effort, output limits, tool schemas, and model-tool round trips for useful-result latency.

### Speculative and authoritative conversation views

The application server derives discrete messages from continuous, overlapping speech. The newest message remains provisional and may change text, timing, or speaker assignment. The UI can consume this speculative view; analytics and safety consume finalized records. OpenAI explicitly treats segmentation as a freshness-versus-certainty trade-off.

### Faster startup

OpenAI reports reducing media/data startup from six network round trips to one through WebRTC Abridged Roundtrip Protocol (WARP): DTLS over ICE through SPED, DTLS 1.3, pre-negotiated SCTP through SNAP, and pre-negotiated data channels instead of DCEP. Instant Connect removes SDP exchange from the critical path by pre-negotiating parameters while retaining standard signaling as fallback.

### Read-only shadow rollout

OpenAI silently mirrored a gradually increasing share of production sessions to the new read-only path while the old system served users. This exposed CPU handlers, queues, networking, geography, long-session memory pressure, reconnects, compaction, shutdown races, metric ambiguity, unhealthy-engine masking, and configuration drift.

## OpenAI-specific choices that are not general Fonely facts

- A native full-duplex GPT-Live model can control conversational timing; Fonely's current STT→text LLM→TTS cascade cannot simply delete endpointing.
- OpenAI's Go rewrite was justified by ChatGPT-scale frame scheduling; it is not evidence that Fonely must leave Pipecat/Python before measurement.
- WARP requires transport implementation support. SmallWebRTC/aiortc does not thereby become WARP-capable.
- Seamless model-instance migration and KV-cache handoff solve long-running frontier-scale inference; they are not an immediate dental-call requirement.
- The article does not establish Tamil/Tanglish, Indian telephony, critical-field, or cost performance.

## Fonely engineering inferences

### The voice must flow, but authority must remain deterministic

Fonely should decouple continuous media from slow intelligence work while preserving its stricter business boundary:

```text
Realtime media plane
├── SmallWebRTC audio input/output
├── VAD, overlap, interruption
├── provisional transcript view
└── Cartesia playback/cancellation

Asynchronous intelligence plane
├── final STT evidence
├── deterministic critical-field state
├── read-only or confirmed tools
├── Claude realization
└── generation-aware output
```

Continuous audio does not grant a model authority over phone, name, date, time, doctor, service, confirmation, mutation, or success.

### Every asynchronous result needs conversational validity

Every delegate, LLM response, and output generation needs `session_id`, `turn_id`, and `generation_id`. Cancellation is best effort; late results must be dropped if their token no longer matches current state.

### Maintain three transcript levels

1. Speculative partial: revisable and UI/prefetch-only.
2. Final provider evidence: immutable raw STT output.
3. Authoritative value: deterministic validation, readback, and explicit confirmation.

### Measure concurrent sessions, not request throughput

Voice capacity must include frame scheduling, queue depth, event-loop delay, memory/session, open provider streams, reconnects, and geographic latency. HTTP requests per second is not an adequate voice capacity metric.

### Startup is an end-to-end product metric

Measure click→transport→provider readiness→first audible greeting. Parallelize safe initialization and preload local assets, but do not send hidden billable provider requests.

## Candidate POC experiments

- Keep microphone/VAD/STT active during Cartesia playback and delayed read-only work.
- Add generation-aware asynchronous delegation and stale-result suppression.
- Expose speculative, final, and authoritative transcript states separately.
- Instrument startup milestones and compare cold versus local-warm modes.
- Run deterministic provider-free concurrent-session simulation.
- Exercise barge-in and cancellation in the actual SmallWebRTC lab.

## Non-claims

This research does not prove:

- GPT-Live integration or equivalence;
- WARP support in Fonely;
- native acoustic/emotion reasoning in the current cascade;
- Tamil/Tanglish or 8 kHz performance;
- production backend mutation safety;
- staging, pilot, or production readiness.
