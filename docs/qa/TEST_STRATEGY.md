# Fonely Testing Strategy

This document defines the testing pyramid for the Fonely platform: the levels of
verification, what each level proves, and what it deliberately leaves to levels
above it.

---

## 1. Pure Domain Unit Tests

Fast, in-process tests that run without a database, network, or external
service.

### What they prove

- Deterministic business logic (price calculation, duration math, buffer
  arithmetic).
- State-machine transitions (hold -> confirmed -> cancelled) and rejection of
  illegal transitions.
- Input validation and normalization (phone numbers, durations > 0, quantity
  precision).
- Edge-case arithmetic (zero buffer, split-shift boundary, midnight rollover).
- Serialization round-trips for value objects and DTOs.

### What they do not prove

- Database constraint enforcement (CHECK, EXCLUDE USING gist, unique indexes).
- Behavior under concurrent transactions or row-level locking.
- Correctness of SQL queries or migrations.
- Integration with external providers (STT, LLM, TTS, telephony).

### Guidelines

- No I/O. Replace repositories and gateways with in-memory fakes.
- Target sub-second execution for the entire suite.
- Cover every branch of every domain function; these tests are the cheapest
  safety net.

---

## 2. PostgreSQL Integration Tests

Tests that execute against a real PostgreSQL instance (typically a disposable
Docker container) to verify behavior that only the database can enforce.

### What they prove

- CHECK constraints fire on invalid data (negative stock, zero duration).
- UNIQUE and exclusion constraints prevent duplicates and overlapping
  appointments.
- Concurrent transactions behave correctly: exactly one writer wins a contested
  slot or the last unit of stock; the loser receives a well-defined error.
- Migrations apply cleanly on an empty database and on a database at every
  prior version.
- Trigger and function behavior (e.g., updated_at stamps, hold-expiry cleanup).
- Query plans remain stable after index changes.

### What they do not prove

- HTTP API routing, authentication, or serialization.
- Voice pipeline quality or latency.
- Third-party provider behavior.

### Guidelines

- Each test runs inside a transaction that is rolled back, or uses a fresh
  schema, to guarantee isolation.
- Use explicit savepoints when testing rollback scenarios (multi-item
  reservation failure).
- Tag these tests so they can be excluded from the fast feedback loop when
  developers only change domain code.

---

## 3. Provider Contract Tests

Tests that verify the application's integration layer against STT, LLM, and
TTS providers without making real network calls.

### Approach

- Record real provider responses as fixtures (JSON, audio files) during a
  manual capture session.
- Replay those fixtures through the adapter layer to confirm that the
  application correctly parses responses, handles errors, and respects
  timeout/retry contracts.
- Validate request serialization: the outgoing payload matches the provider's
  expected schema.

### What they prove

- The adapter code correctly maps provider-specific response shapes into
  Fonely's internal domain types.
- Error handling (HTTP 429, 500, malformed JSON, partial audio) produces the
  expected fallback behavior.
- Timeout and retry policies are exercised.

### What they do not prove

- Actual provider uptime or response quality.
- Model accuracy, latency, or cost at runtime.

### Guidelines

- Re-record fixtures periodically (at least once per provider API version
  bump) to catch schema drift.
- One fixture set per provider (Sarvam STT, Sarvam TTS, Fish Audio TTS,
  DeepSeek, Qwen, Llama, etc.).

---

## 4. Conversation and Model Evaluations

Offline evaluation of intent recognition, tool selection, and structured output
compliance using a curated JSONL eval corpus.

### What they prove

- Intent accuracy: the LLM selects the correct intent for a given caller
  utterance.
- Tool selection: the correct function/tool is called with the correct
  arguments.
- Structured output compliance: the LLM response parses into the expected
  schema without post-processing hacks.
- Cross-provider parity: the same eval corpus runs against Sarvam, DeepSeek,
  Qwen, Llama, and any new provider to produce a comparable scorecard.

### Eval corpus format

Each line in the JSONL file contains:

- `input`: the caller utterance (text or transcript).
- `expected_intent`: the correct intent label.
- `expected_tool_calls`: the expected tool name(s) and argument(s).
- `expected_output_schema`: a JSON Schema the response must validate against.
- `language`: the language/dialect tag.
- `tags`: optional tags for slicing results (domain, edge case, ambiguity).

### Metrics

| Metric                    | Definition                                        |
|---------------------------|---------------------------------------------------|
| Intent Error Rate (IER)   | Fraction of utterances where intent is wrong      |
| Tool Selection Accuracy   | Fraction of correct tool + argument combinations  |
| Schema Compliance Rate    | Fraction of responses that validate against schema|

### Guidelines

- Run evals on every LLM provider change, prompt change, or tool schema
  change.
- Store results in a versioned results directory alongside the corpus so
  regressions are visible in code review.

---

## 5. Voice Quality Evaluations

Evaluation of the audio pipeline independent of conversation logic.

### STT (Speech-to-Text)

- **Word Error Rate (WER)**: measured per language and dialect using a labeled
  audio corpus.
- **Intent Error Rate (IER)**: measured after the STT transcript is fed into
  intent classification, capturing compounding errors.
- Test at **8 kHz / mu-law** (telephone quality), not studio-grade audio.

### TTS (Text-to-Speech)

- **Mean Opinion Score (MOS)**: rated by native speakers on a 1-5 scale for
  naturalness.
- **Pronunciation accuracy**: evaluated for domain-specific terms (business
  names, service names, addresses).
- Test the actual audio rendered at telephone bandwidth.

### Latency

- **Time-to-first-audio**: target < 500 ms from end of caller speech to first
  byte of TTS audio.
- **P50 / P95 turn latency**: full STT -> LLM -> TTS pipeline, measured
  end-to-end.

### Guidelines

- Maintain a labeled audio corpus per supported language.
- Run MOS evaluations with a panel of at least 3 native speakers per language.
- Log latency percentiles in CI so regressions are caught before deployment.

---

## 6. End-to-End Telephony Tests

Tests that exercise the full call path through the Exotel AgentStream
integration.

### What they prove

- A real phone call connects, streams audio bidirectionally, and terminates
  cleanly.
- Barge-in: when the caller speaks while TTS is playing, the system stops
  playback and begins listening.
- Silence handling: the system detects prolonged silence and responds with a
  prompt or graceful hang-up.
- DTMF: tone inputs are captured and routed correctly (if applicable).
- Call metadata (duration, status, recording URL) is persisted accurately.

### What they do not prove

- Conversation correctness (covered by model evals).
- Database constraint enforcement (covered by integration tests).

### Guidelines

- Run against a staging Exotel environment, not production.
- Use synthetic callers (SIP soft-phones or Exotel test numbers) to avoid
  telephony costs in CI.
- Record calls for post-hoc review but do not store PII in CI artifacts.

---

## 7. Pilot Acceptance Tests

Validation with real businesses making and receiving real calls, measured
against the pilot scorecard.

### What they prove

- The system works for a specific business type (clinic, salon, shop) in a
  real-world environment.
- Callers can complete their goal (book appointment, place order, get
  information) without human fallback.
- The business owner finds the experience acceptable.

### Pilot scorecard dimensions

- Call completion rate (caller achieved their goal).
- Misunderstanding rate (system asked for clarification or made an error).
- Owner satisfaction (qualitative, collected via debrief).
- Caller satisfaction (post-call survey or inferred from call outcome).

### Guidelines

- Define pass/fail thresholds before the pilot begins.
- Run each pilot for a minimum duration (e.g., 1 week or 50 calls, whichever
  comes first) to gather statistically meaningful data.
- Document every failure mode and feed it back into the eval corpus and
  provider contract tests.

---

## 8. Load and Soak Tests

Tests that verify the system under sustained and peak traffic conditions.

### Transaction contention

- Simulate N concurrent callers booking the same appointment slot or ordering
  the last unit of stock.
- Verify that exactly one caller succeeds and all others receive a clear
  rejection, with no data corruption.

### Provider latency injection

- Inject artificial latency (and occasional errors) into STT, LLM, and TTS
  provider calls.
- Verify that timeouts, retries, and circuit breakers behave as configured.
- Confirm that degraded provider performance does not cascade into database
  connection pool exhaustion.

### Multi-hour stability (soak)

- Run a realistic traffic profile for 4+ hours.
- Monitor for memory leaks, connection leaks, file descriptor exhaustion, and
  growing query times.
- Verify that background workers (hold expiry, reservation cleanup) continue
  to run on schedule.

### Metrics to collect

| Metric                        | Acceptable threshold           |
|-------------------------------|--------------------------------|
| P99 API response time         | < 500 ms (excluding provider)  |
| Error rate under load         | < 0.1%                         |
| Database connection pool util | < 80% at peak                  |
| Memory growth over soak       | < 10% above baseline           |

### Guidelines

- Run load tests against an environment that mirrors production sizing.
- Store results in a time-series database so trends are visible across
  releases.
- Gate releases on load-test pass/fail; do not ship a build that regresses
  P99 latency.
