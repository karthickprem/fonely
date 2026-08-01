# Fonely — Product and Engineering Roadmap

> **Document status:** This is the durable product strategy and phase roadmap. Product flows are targets, not claims of current functionality. See [STATUS.md](STATUS.md) for current evidence and blockers.

## Product thesis

Fonely aims to be a multilingual AI front desk for Indian small businesses. It should answer inbound calls, understand customers in their language, safely handle enquiries and confirmed transactions, and notify owners without requiring a dashboard or technical setup.

The initial customer hypothesis includes appointment businesses such as clinics and salons and finite-stock pickup businesses such as meat/fish shops and bakeries. The first vertical will be selected by credible design-partner commitment and willingness to pay, not by architecture preference.

## Core safety principle

**The model is the ears and mouth. The database is the source of truth. Deterministic code is the gatekeeper.**

The model must not:

- Invent stock, prices, schedules, or availability.
- Calculate authoritative totals.
- Write business tables directly.
- Call internal commit operations.
- Announce transaction success before the deterministic engine commits.

## Target transaction path

```text
Caller speaks
→ speech-to-text
→ model selects a validated public tool
→ application injects tenant and verified caller context
→ PendingAction proposal is created or revised
→ caller hears the authoritative confirmation snapshot
→ caller confirms
→ deterministic engine commits in PostgreSQL
→ committed result is returned
→ text-to-speech announces success
→ owner receives a notification
```

The production backend currently implements the generic pending-action lifecycle for orders and owner stock-update payloads. It does not yet implement the Phase C inventory/order engine, Phase D appointment engine, or production provider/tool dispatcher.

## Lifecycle-safe public-tool contract

The versioned target contract is `evals/tool-contract.v1.json`.

Inventory/order tools:

- `check_inventory`
- `create_pending_order`
- `revise_pending_order`
- `confirm_pending_order`
- `cancel_pending_order`

Appointment tools:

- `check_availability`
- `create_pending_appointment`
- `revise_pending_appointment`
- `confirm_pending_appointment`
- `cancel_pending_appointment`
- `reschedule_appointment`

Information and escalation:

- `get_business_information`
- `escalate_to_owner`

Owner operations use proposal/confirmation pairs, including stock, price, and schedule updates.

Internal operations such as `begin_commit`, `complete_commit`, `fail_commit`, `internal_get`, and `internal_get_active` are application-engine operations and must never be LLM-callable.

These names define a target public boundary; they do not imply that every adapter or deterministic engine is currently implemented.

## Implemented data foundation

The authoritative schema is the SQLAlchemy ORM and Alembic migrations, not a duplicated SQL sketch:

- `backend/src/fonely/models/schema.py`
- `backend/src/fonely/models/enums.py`
- `backend/migrations/versions/0001_initial_schema.py`
- `backend/migrations/versions/0002_pending_action_state_machine.py`
- `backend/migrations/versions/0003_committed_entity_linkage.py`

The foundation models businesses, capabilities/locales/users, operating schedules and exceptions, products, services/resources, inventory balances/reservations/movements, orders and line items, appointments, calls, pending actions, and owner audit events.

Database tables existing in the foundation does not mean their Phase C/D transaction engines are implemented.

## Nine development phases

### Phase A — Production backend foundation

Scope:

- Python backend package and configuration.
- Tenant-aware ORM schema.
- Alembic migrations and parity checks.
- Strict values, enums, quantities, money, and timestamps.
- Safe database/session foundations.

Status: implemented and locally verified; final foundation gate depends on green PostgreSQL CI.

### Phase B — PendingAction lifecycle

Scope:

- Proposal, revision, confirmation, commit, failure, cancellation, rejection, and expiry states.
- Idempotency and optimistic concurrency.
- Canonical payload digests and confirmation snapshots.
- Actor authorization and trusted internal commit boundary.
- Exact committed-entity linkage.

Status: implemented through B.1 hardening and migration `0003`.

### Phase C — Inventory and order engine

Scope:

- Owner stock set/add and walk-in sale.
- Atomic inventory reservations.
- Multi-item all-or-nothing confirmation.
- Price snapshots and authoritative totals.
- Reservation cancellation/expiry release.
- Pickup completion and inventory ledger consistency.
- Final-stock concurrency behavior.

Gate: PostgreSQL CI must be green and a design-partner/vertical decision must justify this as the first engine.

### Phase D — Appointment and scheduling engine

Scope:

- Services, durations, buffers, resources, eligibility, schedules, breaks, and exceptions.
- Deterministic availability.
- Temporary holds and expiry.
- Database-enforced non-overlap.
- Confirmation, cancellation, and safe rescheduling.
- Time-zone and exact interval-boundary rules.

Gate: explicit founder priority and an approved implementation specification. Dev3 may implement this phase only after assignment.

### Phase E — Provider-independent AI and public-tool boundary

Scope:

- Typed STT, model, and TTS ports.
- Strict public-tool registry and dispatcher.
- Structured tool validation and typed results.
- Provider timeout, cancellation, usage, and error contracts.
- Permanent exclusion of internal operations.

Status: target architecture only. No production dispatcher exists.

### Phase F — Production voice pipeline

Scope:

- Telephony audio transport.
- Streaming STT and TTS.
- Conversation orchestration.
- Barge-in, silence/noise, low-confidence clarification, disconnect recovery, and latency controls.
- No-success-before-commit behavior.

Status: a feasibility prototype exists under `src/` and `public/`; production implementation is not started.

### Phase G — WhatsApp owner experience

Scope:

- Business onboarding/configuration.
- Owner stock/schedule management through safe proposal/confirmation flows.
- Transaction notifications and daily summaries.
- Human escalation and correction workflows.

Status: not implemented.

### Phase H — Payments and provisioning

Scope:

- Subscription provisioning and entitlements.
- Business/number activation.
- Quotas, overage handling, and billing records.
- No autonomous end-customer payment in the initial pilot.

Status: not implemented.

### Phase I — Controlled pilot

Scope:

- A small set of consenting design-partner businesses.
- One primary vertical, one deterministic transaction, Tamil/Tanglish plus Indian English, one provider path, and manual monitoring.
- Incident response, human fallback, owner feedback, and measurable go/no-go criteria.

Status: not started.

## Pilot scope hypothesis

Begin with one thin vertical slice and expand only after evidence:

- Approximately 3–5 initial design partners, expandable to 5–10 after stability.
- Inbound calls only.
- Business-hours, overflow, or after-hours forwarding.
- One deterministic transaction type.
- Manual transcript review with consent and PII controls.
- Human owner escalation.
- No diagnosis or medication advice.
- No autonomous payment.
- No outbound marketing.

A credible design partner shares real configuration, participates in weekly testing, accepts monitored calls under consent, provides feedback, and demonstrates willingness to pay after a trial.

## Evaluation and quality gates

The QA.3 corpus contains 211 synthetic cases and 377 turns with zero structural tool-contract mismatches. It is useful for requirements and structural conformance but is not proof of:

- Native-language quality.
- Clinical correctness.
- Provider accuracy.
- Real caller behavior.
- Pilot readiness.

Required evidence layers are described in `docs/qa/TEST_STRATEGY.md`.

## Provider and pricing policy

Provider names, capabilities, quotas, and prices change. Any selection or cost model must include a dated authoritative source and measured usage. Existing prototype integrations or earlier price estimates are not durable commercial facts.

Current policy:

- Keep domain services provider-independent.
- Compare providers using the same reviewed corpus and real-call samples.
- Measure STT/TTS/model/telephony usage and cost per successful transaction.
- Treat customer prices and margin targets as founder hypotheses until pilot data exists.

## What the pilot does not require

- Mobile application.
- Large web dashboard.
- CRM integrations.
- Outbound calling or marketing.
- Delivery logistics.
- General workflow DSL.
- Microservices, Kafka, Kubernetes, multi-region deployment, or every Indian language.
- Multiple verticals before one succeeds.

## Current next sequence

1. Finish Dev2's PostgreSQL CI correction and obtain a green run including migration downgrade/re-upgrade.
2. Independently review Dev1's repository-audit hardening and Dev2's CI correction.
3. Merge focused changes without crossing ownership boundaries.
4. Secure a credible design-partner commitment.
5. Authorize the selected deterministic engine.
6. Implement one thin end-to-end slice.
7. Measure reliability, latency, cost, owner correction, and willingness to pay.

See [STATUS.md](STATUS.md) for the current evidence snapshot.
