# Load Test Plan

Load testing strategy for Fonely. This document defines workload models at each
scale tier and the tests to be implemented.

---

## Workload Models

### Tier 1: 10 Pilot Businesses

Assumptions: 10 businesses onboarded, all active during pilot.

| Metric | Estimate | Notes |
|--------|----------|-------|
| Calls per business per day | 2-5 | Low volume during pilot validation |
| Peak simultaneous calls | 3 | Unlikely all businesses receive calls at once |
| STT sessions per day | ~30 | One per call |
| TTS streams per day | ~30 | One per call (multi-turn within stream) |
| LLM requests per day | ~90 | ~3 tool-call turns per call average |
| Database transactions per day | ~60 | Orders, appointments, stock checks |
| WhatsApp messages per day | ~50 | Notifications to owners + customer confirmations |

Provider capacity limits: UNKNOWN (not measured).

### Tier 2: 100 Businesses

Assumptions: ~60% of businesses active on any given day.

| Metric | Estimate | Notes |
|--------|----------|-------|
| Active businesses per day | ~60 | 60% daily active rate |
| Calls per day | 120-300 | 2-5 calls per active business |
| Peak simultaneous calls | 15 | Concentrated during morning/evening hours |
| STT sessions per day | ~300 | |
| TTS streams per day | ~300 | |
| LLM requests per day | ~900 | |
| Database transactions per day | ~600 | |
| WhatsApp messages per day | ~500 | |

Provider capacity limits: UNKNOWN (not measured).

### Tier 3: 1,000 Businesses

Assumptions: ~40% of businesses active daily. Peak hours see ~20% of daily volume.

| Metric | Estimate | Notes |
|--------|----------|-------|
| Active businesses per day | ~400 | 40% daily active rate |
| Calls per day | 800-2,000 | |
| Peak simultaneous calls | 50-80 | Morning rush (meat shops 5-10am), evening (clinics, salons) |
| STT sessions per day | ~2,000 | |
| TTS streams per day | ~2,000 | |
| LLM requests per day | ~6,000 | |
| Database transactions per day | ~4,000 | |
| WhatsApp messages per day | ~3,000 | |

Provider capacity limits: UNKNOWN (not measured).

### Tier 4: 10,000 Businesses

Assumptions: ~40% daily active. Geographic spread reduces peak concentration somewhat.

| Metric | Estimate | Notes |
|--------|----------|-------|
| Active businesses per day | ~4,000 | |
| Calls per day | 8,000-20,000 | |
| Peak simultaneous calls | 200-500 | Multiple regional peaks may overlap |
| STT sessions per day | ~20,000 | |
| TTS streams per day | ~20,000 | |
| LLM requests per day | ~60,000 | |
| Database transactions per day | ~40,000 | |
| WhatsApp messages per day | ~30,000 | |

Provider capacity limits: UNKNOWN (not measured).

---

## Test Definitions

Each test below is defined for future implementation. None have been executed
yet. Target tooling: k6, Locust, or custom async Python harness.

### Test 1: API Request Load

**Goal:** Determine maximum sustainable HTTP request throughput for application
endpoints (health check, pending action CRUD, business queries).

**Method:**
- Ramp from 10 to 200 requests/second over 5 minutes.
- Sustain peak for 15 minutes.
- Measure P50, P95, P99 response latency and error rate.
- Record database connection pool utilization.

**Pass criteria:** P95 latency < 500ms, error rate < 0.1% at target tier load.

**Status:** Not implemented.

### Test 2: WebSocket Connection Load (STT Streaming)

**Goal:** Verify the system can sustain the target number of concurrent WebSocket
connections for real-time speech-to-text streaming.

**Method:**
- Open N concurrent WebSocket connections (N = peak simultaneous calls for tier).
- Stream pre-recorded audio segments through each connection.
- Measure connection establishment time, message latency, and drop rate.
- Monitor server memory and file descriptor usage.

**Pass criteria:** Zero connection drops, message latency < 200ms at target
concurrency.

**Status:** Not implemented.

### Test 3: Transaction Contention -- Concurrent Inventory Reservations

**Goal:** Verify that concurrent order commits for the same product do not
oversell inventory.

**Method:**
- Seed a product with 10 units of stock.
- Launch 20 concurrent order-commit requests, each requesting 1 unit.
- Verify exactly 10 succeed and 10 are rejected with `insufficient_stock`.
- Verify final inventory balance is exactly 0.
- Verify no negative stock via check constraint violation.

**Pass criteria:** Zero oversells. No constraint violations. All rejections
return correct error codes.

**Status:** Not implemented.

### Test 4: Appointment Hot-Slot Race

**Goal:** Verify that concurrent appointment booking requests for the same
time slot do not result in double-booking.

**Method:**
- Create a resource with a single available slot (e.g., 10:00-10:30).
- Launch 10 concurrent appointment-commit requests for that slot.
- Verify exactly 1 succeeds and 9 are rejected.
- Verify the database contains exactly 1 confirmed appointment for that slot.

**Note:** Until the PostgreSQL exclusion constraint is deployed (Phase D of
implementation plan), this test is expected to expose the double-booking gap.
The current `ix_appointments_resource_lookup` index does not prevent overlapping
appointments.

**Pass criteria (post-Phase D):** Zero double-bookings. Exactly 1 winner.

**Status:** Not implemented.

### Test 5: Inventory Final-Stock Race (Last-Unit Contention)

**Goal:** Stress-test the last-unit scenario where multiple callers attempt to
reserve the final unit of a product simultaneously.

**Method:**
- Seed a product with exactly 1 unit of stock.
- Launch 50 concurrent reservation requests.
- Verify exactly 1 reservation succeeds.
- Verify the `ck_inv_on_hand` and `ck_inv_reserved_lte_on_hand` check
  constraints are never violated.
- Repeat 100 times to ensure no race window.

**Pass criteria:** Zero oversells across all iterations.

**Status:** Not implemented.

### Test 6: Provider Latency Injection

**Goal:** Verify system behavior when upstream providers (Sarvam STT, Sarvam
LLM, Sarvam TTS) respond slowly.

**Method:**
- Inject artificial latency into provider API calls:
  - STT: add 3s, 5s, 10s delay.
  - LLM: add 3s, 5s, 10s delay.
  - TTS: add 3s, 5s, 10s delay.
- Run a standard call flow through the pipeline.
- Measure end-to-end call latency and caller experience.
- Verify timeouts trigger gracefully (no hung connections or orphaned state).

**Pass criteria:** System returns a timeout error or degraded response rather
than hanging indefinitely. No orphaned pending actions left in COMMITTING state.

**Status:** Not implemented.

### Test 7: Provider Outage Simulation

**Goal:** Verify system resilience when a provider returns 503 or times out
completely.

**Method:**
- Configure mock provider to return HTTP 503 for all requests.
- Attempt a call flow.
- Verify the caller receives a polite error message.
- Verify no data corruption (no half-committed orders or appointments).
- Verify pending actions are left in a recoverable state.

**Variants:**
- Sarvam STT outage (cannot transcribe caller speech).
- Sarvam LLM outage (cannot determine intent).
- Sarvam TTS outage (cannot generate speech response).
- Exotel outage (cannot receive calls -- out of scope for application-level test).

**Pass criteria:** No data corruption. Caller hears a fallback message or call
is terminated cleanly. Alert fires within 60 seconds.

**Status:** Not implemented.

### Test 8: Database Failover

**Goal:** Verify application behavior during a PostgreSQL failover event.

**Method:**
- Run a sustained call load (10 concurrent calls).
- Trigger database failover (promote replica, shut down primary).
- Measure: how many in-flight calls fail, recovery time, data integrity.

**Pass criteria:** In-flight calls fail with a retriable error (not data
corruption). New calls succeed within 30 seconds of failover completion.
Zero data loss.

**Status:** Not implemented.

### Test 9: Worker Restart During Active Call

**Goal:** Verify that an application process restart during an active call
results in a clean outcome (not a half-committed transaction).

**Method:**
- Start a call flow that is mid-commit (COMMITTING state).
- Kill the worker process.
- Restart the worker.
- Verify the pending action is in a recoverable state (COMMITTING, not CONFIRMED
  with missing entity).
- Verify the caller's call is terminated (Exotel handles disconnect).

**Pass criteria:** No orphaned confirmed entities without matching pending
action completion. Database state is consistent after restart.

**Status:** Not implemented.

### Test 10: Multi-Hour Soak Test

**Goal:** Verify system stability under sustained realistic load over extended
periods.

**Method:**
- Simulate the Tier 2 workload model (15 concurrent calls, mixed operations).
- Run continuously for the target duration.
- Monitor: memory usage, database connection pool, file descriptors, response
  latency percentiles, error rate.

**Durations:**
- 8-hour soak: minimum for Stage 3 readiness.
- 24-hour soak: recommended for Stage 3.
- 48-hour soak: required for Stage 4 readiness.

**Pass criteria:**
- Memory usage stable (no monotonic increase > 10% over baseline).
- Database connection pool utilization < 80%.
- P95 latency does not degrade > 20% compared to first hour.
- Error rate < 0.1% sustained.
- Zero connection leaks.

**Status:** Not implemented.

---

## Provider Capacity Limits

The following limits are UNKNOWN and must be measured or confirmed with providers
before scaling beyond Tier 1:

| Provider | Limit Type | Current Knowledge |
|----------|-----------|-------------------|
| Sarvam STT | Max concurrent WebSocket connections | UNKNOWN |
| Sarvam STT | Requests per second | UNKNOWN |
| Sarvam LLM | Requests per second | UNKNOWN |
| Sarvam LLM | Tokens per minute | UNKNOWN |
| Sarvam TTS | Requests per second | UNKNOWN |
| Sarvam TTS | Concurrent streams | UNKNOWN |
| Exotel AgentStream | Max concurrent calls per account | UNKNOWN |
| Exotel AgentStream | Max concurrent WebSocket connections | UNKNOWN |
| WhatsApp Business API | Messages per second | UNKNOWN |
| WhatsApp Business API | Messages per day per business | UNKNOWN |

These limits should be documented as they are discovered during pilot operations
and provider discussions.
