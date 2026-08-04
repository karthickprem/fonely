# GPT-Live-Inspired POC Results V1

Freeze ID: `GPT-LIVE-INSPIRED-POC-V1`
Promotion status: evidence only.

## Automated evidence

- 36 focused Python tests passed.
- 75 existing JavaScript voice regressions passed.
- Client production build passed with Bun runtime.
- POC schemas validated during tests and runner execution.

## Provider-free logical-time soak

Frozen schedule:

- 25 independent sessions;
- 100 turns per session;
- 2,500 turns per run;
- 3 identical reruns.

Observed summary:

```json
{
  "cross_session_leaks": 0,
  "deterministic": true,
  "leaked_tasks": 0,
  "stale_results": 1875,
  "stale_suppressed": 1875,
  "turns": 2500
}
```

This proves deterministic contract/state isolation only. It is not a WebRTC/provider concurrency result.

## Real application observation

The SmallWebRTC app was launched with a synthetic microphone fixture and POC delegate enabled.

Observed browser state:

- status: Fonely speaking;
- microphone: live while playback active;
- playback: `Active · mic still live`;
- transcript stage: final evidence;
- generation advanced during overlap;
- no browser exceptions;
- Sarvam transcripts, Claude responses, and Cartesia audio continued across multiple turns.

Observed Cartesia TTFB samples were approximately 76–100 ms. The test also exposed an environment/configuration issue: the first Claude request used a gateway path missing its required trusted user header and returned HTTP 400, while later requests succeeded. This is not accepted as a clean full-session gate and must be corrected in the runtime environment before any readiness claim.

## Failed or incomplete gates

- p95 interruption-to-audible-stop ≤350 ms was not measured from actual last audible PCM.
- server telemetry initially double-counted generation advancement because both interruption and user-start frames were observed; implementation was corrected to advance only on `InterruptionFrame`, but the corrected runtime needs another live measurement.
- delayed delegate events were not observed in the first run because finalized evidence is produced at the context boundary; implementation was corrected and requires another live run.
- startup cold/warm sample counts were not completed.
- Firefox/device/echo-cancellation coverage was not run.
- no real WebRTC multi-session soak was run.

## Conclusion

The architecture and provider-free state model are implemented and promising, but the real-app acceptance suite is incomplete. This POC is not staging-, pilot-, or production-ready.
