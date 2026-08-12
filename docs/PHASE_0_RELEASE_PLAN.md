# Fonely Phase 0 Release Plan

**Status:** Active shipping plan  
**Owner:** Karthick (founder) with joint execution by fonely-ceo and cofounder2  
**Pilot market:** Independent dental clinic in Tamil Nadu  
**Durable product:** Configurable multilingual AI receptionist for multiple businesses and verticals  
**Last updated:** 2026-08-12

## 1. Release definition

Phase 0 is one complete, commercially meaningful vertical slice:

> One dental clinic, one phone number, a natural Tamil-first receptionist, one correctly committed appointment, conservative safety escalation, and durable patient/owner notification evidence.

The clinic is configured through the supported onboarding path. A patient calls the clinic number, completes a Tamil or Tanglish conversation, and receives an outcome backed by authoritative PostgreSQL state. A natural transcript without the correct committed row is a failed booking.

## 2. Scope

### Included

- Tamil-first and practical Tanglish conversation.
- Clinic identity, location, hours, services, configured prices, dentists/resources, and availability.
- Appointment proposal, explicit confirmation, deterministic commit, cancellation/rescheduling only where already proven.
- Closed-hours and unavailable-slot refusal with authoritative alternatives.
- Correct handling of date, time, meridiem, dentist name, corrections, ambiguity, interruption, hangup, and retry.
- Patient and owner notification evidence, delivery, retry, and failure state.
- Human escalation/callback capture.
- Conservative dental safety language; no clinical advice.
- DPDP notice before conversational speech capture, with durable versioned evidence.
- Trusted tenant/call admission, idempotent provider callbacks, PII-safe logs, retention, readiness, backup/restore, rollback, monitoring, and support controls.

### Excluded unless separately proven and authorized

- Diagnosis, medication/dosage, report/X-ray interpretation, treatment promises, or postoperative advice.
- Payments, subscriptions, dashboards, multiple launch verticals, additional providers for optionality, microservices, multi-region infrastructure, or broad platform abstractions.
- Public customer traffic before staging validation and founder approval.

## 3. Non-negotiable invariants

1. PostgreSQL is authoritative for tenants, configuration, appointments, committed evidence, and notification manifests.
2. Tenant, actor, role, call identity, service, price, resource, and business facts never come from model output or untrusted stream fields.
3. A call is admitted only through a provider call row written from our authenticated webhook and a database-owned channel identity.
4. The voice path uses one booking commit route: booking gate → resolver → injected command port → appointment application service.
5. DPDP notice playback and evidence persistence complete before STT accepts conversational speech.
6. No success is reported before the outer database transaction commits.
7. Retry, duplicate callback, disconnect, timeout, provider error, and shutdown converge without duplicate bookings or leaked tasks.
8. Every acceptance PostgreSQL run uses a private `fonely_test_<suffix>` database, verifies `current_database()` and quietness, captures the real exit code, and drops the database afterward.
9. Executed, failed, blocked, skipped, and not-run are distinct states.
10. Voice remains off by default until hosted exact-SHA telephone proof passes.

## 4. Active integration state

The durable WIP branch is:

`origin/integration/fonely-voice-whatsapp-wip`

It currently combines:

- Accepted booking/conversation work through Dev3 D3-M4.
- Database-backed WhatsApp channel identity (`0016`).
- Provider call identity and tenant-bound audio admission (`0017`).
- Validated/preserved Exotel opening metadata.
- Full Dev4 voice runtime and real-STT corrections.
- DPDP foundations.
- Deployment and retention-worker strengthening.
- Locked dependencies, repository-wide Ruff/format/MyPy corrections, and migration parity through the current head.

`main` remains unchanged until the frozen candidate passes the integration gates below.

## 5. Shipping gates

### Gate A — Complete admitted Exotel → canonical voice runtime

Required production path:

```text
Authenticated Exotel callback
→ durable provider call identity
→ tenant-bound stream admission
→ validated Exotel start metadata
→ DPDP notice playback
→ durable notice evidence
→ speech input gate opens
→ Sarvam STT
→ shared conversation and booking logic
→ deterministic confirmation
→ PostgreSQL appointment commit
→ Cartesia TTS
→ Exotel media output
→ notification evidence/delivery
→ clean teardown
```

#### Admission/transport responsibilities

- Validate optional `connected` and exactly one bounded `start` event.
- Preserve `stream_sid`, provider CallSid, encoding, declared rate, and channels; consumed opening frames must not disappear.
- Tenant identity comes only from the admitted database call/channel identity. Frame `To`, `From`, URL, and model data are consistency inputs only.
- Reject malformed, oversized, unsupported, replayed, mismatched, unknown, disabled, and ended sessions deterministically.
- Handle JSON/base64 media, DTMF, stop, interruption/clear, disconnect, and rate negotiation.
- Capacity limits are overload controls, never tenant authorization.

#### Voice runtime responsibilities

- Mount one typed `handle_audio_session` adapter; do not redo admission.
- Build the Exotel serializer/transport from validated metadata and actual rate.
- Compose provider services, context aggregation, `BookingStateInjector`, and `BookingPostLLMGate`.
- Carry trusted call ID into the existing command port; never add a second commit implementation.
- Enforce notice audio → playback complete → evidence persisted → greeting → input/STT open.
- If notice synthesis/playback/evidence fails, input remains closed and the caller receives a short spoken failure/handoff message before hangup.
- Barge-in during notice is not treated as understood speech; greeting re-invites the caller after the gate opens.
- Stop/disconnect/timeout/provider failure/cancellation/shutdown release workers, counters, clients, and sockets exactly once.

#### Platform responsibilities

- Disabled voice refuses explicitly; it never accepts and silently drains a patient call.
- Enabled voice validates Exotel, Sarvam, Anthropic, Cartesia, voice ID, Pipecat, database, and channel prerequisites at startup.
- Explicitly enabled but misconfigured voice fails startup.
- Mount runtime only after successful initialization.
- Shutdown stops new admissions and drains/cancels active sessions within the configured timeout before HTTP/database resources close.

### Gate B — Freeze one exact candidate

Before freeze:

- Complete the runtime milestone.
- Obtain independent verdict for Dev3 item #19; integrate only accepted ancestry after D3-M4.
- Finish Dev2 branch review.
- Keep rejected Dev1/Dev5 implementations out; any extracted scenarios require separate frozen directives and mutation proof against canonical code.
- Account for every branch/worktree as integrated, pending review, rejected, superseded, experimental, or disposable.

After freeze, no feature additions. Only defects blocking the Phase 0 path may change the candidate.

### Gate C — Exact-candidate engineering verification

Run exactly the repository/CI gates, not narrower approximations:

- Fresh `uv sync --locked --all-extras` from an empty environment.
- `ruff check .` and `ruff format --check .` from `backend/`.
- Strict `mypy src`.
- Eval schema/profile validation.
- Complete non-live suite with runtime warnings treated as errors.
- Complete voice unit/integration suite; live tests must skip explicitly without credentials.
- Exotel authentication/protocol/admission, DPDP ordering, tenant isolation, retry/idempotency, notification, retention, and lifecycle tests.
- Secret/artifact audit.

### Gate D — Private PostgreSQL and migration evidence

For the frozen SHA:

1. Create a unique local database matching `fonely_test_<suffix>`.
2. Query and record `current_database()` and `pg_stat_activity`; no other suite may touch it.
3. Assert one migration head derived from the scripts directory.
4. Fresh upgrade to head.
5. Populated previous-head → current-head upgrade.
6. Full PostgreSQL suite.
7. Downgrade/re-upgrade with populated-data guards and remediation evidence.
8. ORM/migration parity and `alembic check`.
9. Disposable backup/restore where PostgreSQL client binaries exist; otherwise GitHub CI is the first execution and the local state remains NOT RUN.
10. Drop the private database.

### Gate E — Build and execute deployment artifacts

- Render base and public Compose configuration.
- Build the exact candidate image and record immutable image ID/digest.
- Start PostgreSQL, migration, API, inbound worker, notification worker, retention worker, and Caddy.
- Prove database-aware readiness and worker presence.
- Prove public edge exposes only approved provider paths and liveness; private/internal/metrics/readiness/OpenAPI/unknown paths return edge 404.
- Run synthetic onboarding and booking proof through the supported API and database-backed channel registration.
- Exercise retention using expired synthetic records.
- Rehearse stop, restart, rollback/forward-fix, and secret rotation procedure.

A static deployment test is not container evidence. Docker/Compose steps remain NOT RUN until executed.

### Gate F — Hosted staging

After Karthick supplies the host and approved secret locations:

- Indian-region VM with public IPv4.
- DNS A record and TLS.
- Exact tested image digest.
- Secrets installed on host, never pasted into chat or committed.
- PostgreSQL storage/backup configuration.
- Exotel and WhatsApp callback configuration.
- Centralized redacted logs and actionable alerts.
- Public-edge check on the real hostname.
- Provider authentication carrier, CallSid/start-frame shape, codec/rate/order, media output, and clear behavior verified through a sandbox call.

### Gate G — Controlled real-call scenarios

Run internal consenting calls before customer traffic:

1. Tamil booking.
2. Tanglish booking.
3. Closed hours.
4. Unavailable slot with alternatives.
5. Date/time/dentist correction.
6. Multi-word Tamil confirmation.
7. Ambiguous/unknown dentist, fail closed.
8. Interruption/barge-in.
9. Hangup/retry/duplicate callbacks.
10. Urgent dental safety escalation.
11. Provider/STT/TTS failure and spoken handoff.

For every successful booking, assert from PostgreSQL/evidence—not transcript:

- Correct tenant, call, service, dentist/resource, day, and time.
- Exactly one confirmed appointment.
- Idempotent replay, no duplicate.
- DPDP notice timestamp/version/locale/content digest.
- Notification manifest and patient/owner status.
- No cross-tenant state or PII leakage.

### Gate H — Native Tamil, reliability, and economics

Karthick reviews:

- Tamil/Tanglish naturalness, Chennai register, names/numbers/date/time, interruptions, DPDP notice, and failure/handoff wording.

A practicing dentist reviews medical escalation and no-advice boundaries.

Measure:

- End-to-end p50/p95 and STT/LLM/TTS segments.
- Concurrent calls and hot-slot contention.
- Worker/provider/database failure recovery.
- Short soak followed by an eight-hour soak.
- Raw usage and actual provider/telephony cost per successful booking.
- Notification delivery, support burden, and operator repair procedure.

### Gate I — One controlled clinic pilot

Pilot controls:

- One named clinic and one number.
- Limited/overflow hours, low daily call cap, human fallback, incident shutdown switch, founder monitoring, and support contact.
- Owner-approved services, prices, dentists, schedules, channel identities, and escalation destination.
- Explicit privacy/consent handling.

Pilot metrics:

- Calls answered, booking completion and correctness, escalations, notification delivery, p50/p95 latency, cost per booking, recovered enquiries, owner satisfaction, patient complaints, manual correction/support effort, and willingness to pay.

## 6. Ownership

### Karthick

- Provider/hosting accounts, spend limits, DNS, external/customer decisions, native Tamil review, dentist relationship, and final launch authorization.

### fonely-ceo

- Single developer-directive owner; admission/start metadata, migration integrity, security/provider-edge corrections, and frozen developer checklists.

### cofounder2

- Integration branch, branch accounting, exact-SHA gates, deployment/release evidence, independent readiness decision with CEO, and authorized push/deployment execution.

### Dev3

- Text conversation/booking correctness, date/time and resource-name normalization, ambiguity and fail-closed behavior, row-level resource evidence; no voice/telephony overlap.

### Dev4

- Pipecat runtime composition, provider services, Tamil/Tanglish voice behavior, DPDP input gate, barge-in/lifecycle, real-audio/latency evidence; no tenant admission or parallel booking engine.

## 7. Founder inputs required before hosted validation

Do not paste credentials into chat. Install them through the approved host secret/environment mechanism and provide only the location/access context.

- Exotel number with media streaming enabled and confirmed authentication method.
- Indian-region host with public IPv4 and authorized access.
- DNS A record.
- Sarvam and Cartesia runtime credentials.
- One explicitly selected, production-reachable LLM endpoint and credential. GPT-5.6 Luna currently depends on AMD's corporate gateway and is not deployable until that gateway is reachable and approved from the production host; otherwise Karthick must select a reachable Phase 0 provider. Missing or unreachable selected-provider configuration is a deployment failure, never an implicit fallback.
- WhatsApp Business/Cloud API number and credentials.
- Approved provider/hosting spend cap.
- Time for native Tamil and DPDP notice review.

## 8. Release states and go/no-go

- **Integrated:** exact candidate merged and full automated/private-DB gates pass.
- **Staging-validated:** hosted artifact, provider edge, operations, and controlled real calls pass.
- **Pilot-validated:** one clinic uses it successfully under controlled conditions.
- **Production-ready:** reliability, privacy, backup/restore, incident/support, load/soak, and economics are proven.

Phase 0 may enter a controlled pilot only when it is staging-validated, has no unresolved P0 defect, has founder/native/dentist approvals, and has an executable rollback/shutdown path.

## 9. Active execution order

1. Complete validated Exotel metadata handoff and admitted runtime mounting.
2. Finish Dev3 item #19 review and integrate only if accepted.
3. Finish Dev2 branch classification and archive selected lab evidence.
4. Freeze candidate SHA.
5. Run exact repository, private PostgreSQL, migration, and backup/restore gates.
6. Build and execute containers.
7. Deploy to the founder-provided host.
8. Run provider-edge and controlled real-call matrix.
9. Complete native Tamil/dentist/reliability/economic review.
10. Start one supervised clinic pilot.

This plan is the shipping authority. Scope changes require a joint CEO/cofounder2 decision, and outward/customer commitments require Karthick.
