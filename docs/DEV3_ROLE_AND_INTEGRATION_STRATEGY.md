# Dev3 Implementation Role and Integration Boundaries

## Authority

`docs/TEAM_AND_OPERATING_MODEL.md` is authoritative. `docs/STATUS.md` contains the current assignment and phase gate.

## Role

Dev3 is Fonely's **Principal Integration and Voice Platform Developer**.

Dev3 is an implementation developer only. Dev3 does not independently review other developers, approve readiness, define engineering phase gates, or own product decisions. The AI cofounder owns requirements, specifications, acceptance criteria, independent review, and phase approval; the founder retains final business authority.

## Implementation responsibilities

After explicit assignment and approval, Dev3 may implement:

- Provider-independent STT, model, and TTS interfaces/adapters.
- The strict lifecycle-safe public-tool registry and dispatcher.
- Conversation orchestration and typed provider/tool results.
- Timeout, cancellation, low-confidence clarification, barge-in, disconnect recovery, and no-success-before-commit behavior.
- PII-safe logging, trace correlation, usage accounting, and pilot observability.
- One approved thin end-to-end vertical slice.
- Later, the deterministic appointment engine under a separately approved Phase D specification.

Dev3 self-tests and reports evidence. Independent acceptance remains with the AI cofounder.

## Possible implementation ownership

Only after explicit transfer:

```text
backend/src/fonely/providers/**
backend/src/fonely/conversation/**
backend/src/fonely/tooling/**
backend/tests/contract/providers/**
backend/tests/unit/conversation/**
backend/tests/unit/tooling/**
```

Possible later Phase D ownership:

```text
backend/src/fonely/domain/appointments/**
backend/src/fonely/repositories/appointments.py
backend/src/fonely/services/appointments.py
appointment-specific migrations and tests
```

Dev3 must not modify Dev1- or Dev2-owned areas unless ownership is explicitly transferred for a bounded task.

## Non-responsibilities

Dev3 does not:

- Decide the first vertical, pricing, spending, deployment, or customer policy.
- Write or approve its own acceptance criteria.
- Serve as an independent product/architecture reviewer.
- Approve Phase C, Phase D, provider readiness, or pilot readiness.
- Connect the voice prototype directly to internal commit operations.
- Implement tools whose deterministic backend service does not exist and then simulate success.

## Required integration invariants

Any Dev3 implementation must preserve:

1. Tenant and verified actor context is injected by the application, never supplied by the model.
2. Public operations are allowlisted from the lifecycle-safe contract.
3. `begin_commit`, `complete_commit`, `fail_commit`, `internal_get`, and `internal_get_active` are never public or LLM-callable.
4. Stock, price, schedule, duration, and availability come from deterministic services and PostgreSQL.
5. The caller hears success only after the database transaction commits.
6. Provider failures do not fabricate transaction success.
7. Retries use stable identifiers and idempotency policies.
8. PII and credentials are not stored in prompts, logs, or conversation state beyond approved needs.

## Current state

PostgreSQL CI is green as of run `30687004089`, but Dev3 still has no approved implementation workstream. The first deterministic transaction engine has not been selected or authorized. Dev3 should wait for an explicit implementation assignment based on an AI-cofounder-approved specification and stable domain ports.

## Future sequence

```text
Green PostgreSQL CI
→ credible design-partner/vertical decision
→ one deterministic engine implemented and approved
→ Dev3 receives approved provider/tool/conversation specification
→ Dev3 implements integration against stable domain ports
→ independent review
→ one monitored thin vertical slice
```

See `docs/STATUS.md` for the current evidence and next gate.
