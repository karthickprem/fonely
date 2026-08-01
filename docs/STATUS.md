# Fonely — Current Project Status

**Evidence snapshot:** 2026-08-01 CI timestamps, reviewed on 2026-08-01 in the current project session.

This is the authoritative volatile-status document. Product strategy lives in `PLAN.md`; historical checkpoints live under `docs/daily/`.

## Executive status

```text
Phase A — Backend foundation:               implemented; PostgreSQL CI gate passed
Phase B/B.1 — PendingAction lifecycle:      implemented through migration 0003
QA.3 — Structural evaluation conformance:   passing (211 cases / 377 turns / 0 mismatches)
PostgreSQL CI:                              green on run 30687004089
Phase C — Inventory/order engine:           technically eligible for specification/authorization; not started
Later phases:                               not started as production implementation
```

Fonely is not production-ready, provider-validated, clinically reviewed, or pilot-validated.

## Repository evidence

```text
main baseline:                 231cabb  docs: record initial repository push
Dev2 cache correction:         8d75733  ci: update cache action pin
Dev2 loop-scope correction:    b5d7312  test(postgres): align async fixture loop scope
Alembic head:                  0003
```

`8d75733` and `b5d7312` are on the Dev2 feature branch at this snapshot; they are not recorded here as merged to `main`.

## Latest independently inspected CI evidence

### Run 30685195177 — cache correction

URL: https://github.com/karthickprem/fonely/actions/runs/30685195177

The cache correction allowed CI to pass setup, dependency installation, QA validation, Chennai coverage, package import, Ruff, formatting, MyPy, migration upgrade/check, and 280 non-PostgreSQL tests. PostgreSQL then executed 23 contracts:

```text
1 passed
22 failed with async cross-event-loop / closed-loop fixture errors
```

### Run 30686343063 — loop-scope correction

URL: https://github.com/karthickprem/fonely/actions/runs/30686343063

The async loop-lifetime correction removed the shared event-loop failure. PostgreSQL improved to 22 passes and one stale migration-head expectation failure (`0002` versus implemented head `0003`).

### Run 30687004089 — final green PostgreSQL gate

URL: https://github.com/karthickprem/fonely/actions/runs/30687004089

Commit `40e3fbb` renamed the misleading migration test and aligned its current-head assertion to `0003`. Independent review found no issues. The workflow completed successfully:

```text
Non-PostgreSQL tests:          281 passed, 23 deselected
PostgreSQL integration tests:   23 passed, 281 deselected
Migration downgrade to base:    passed
Migration re-upgrade to 0003:   passed
QA/static/migration checks:     passed
```

The PostgreSQL foundation gate is now green. Expected database errors emitted by negative constraint tests are test evidence, not workflow failures. A non-blocking cache-save warning remains because the configured cache path did not exist; dependency installation still passed and this does not reopen the PostgreSQL gate.

## Backend status

Implemented and locally reviewed:

- Tenant-aware ORM foundation and migrations `0001`–`0003`.
- Deterministic pending-action state machine.
- Strict payload/schema validation and canonical SHA-256 digests.
- Idempotent creation and optimistic version checks.
- Actor-authorized public reads and membership-backed owner/manager authorization.
- Trusted internal commit commands excluded from public operation exposure.
- Exact tenant/entity/pending-action linkage on commit completion.
- Safe migration `0002` legacy-row validation/backfill.
- Unique pending-action linkage for orders and appointments.
- Destructive PostgreSQL test guards.

Not implemented as production behavior:

- Inventory/order transaction engine (Phase C).
- Appointment/scheduling engine and overlap exclusion (Phase D).
- Production public-tool dispatcher.
- Production provider adapters and conversation orchestration.
- WhatsApp owner workflow, payments/provisioning, or controlled pilot.

## Evaluation status

QA.3 structural baseline:

```text
Cases:                   211
Turns:                   377
Tool-contract mismatches:  0
Caller turns with null outcome: 0
Chennai-pilot profile:   passing
All-India profile:       expected future gaps in hi-IN, te-IN, kn-IN, ml-IN
```

Every case remains language-synthetic, domain-unreviewed, and pilot-untested unless its provenance fields later say otherwise. Medical cases also require clinical review. Structural conformance is not provider-quality or production evidence.

## Current work ownership

### Dev1 — developer

- Owns domain/backend implementation and backend dependency correctness.
- Current isolated assignment: harden the repository audit on `dev1/harden-pre-push-audit`.
- Must not modify Dev2's CI/PostgreSQL work or begin Phase C.

### Dev2 — developer

- Owns CI, PostgreSQL verification infrastructure, QA tooling/corpus maintenance, and operational test tooling.
- PostgreSQL CI correction is complete: cache pin, async loop scope, migration-head contract, 23 contracts, and downgrade/re-upgrade are verified green.
- Next assignment should remain a separately approved CI/QA/infrastructure task; Dev2 must not begin domain Phase C work.

### Dev3 — developer only

- Has no independent review, specification-approval, or phase-gate authority.
- Future approved scope: provider adapters, public-tool dispatch, conversation orchestration, thin-slice integration, and later the appointment engine.
- Implementation begins only after an explicit assignment against an AI-cofounder-approved specification.

### AI cofounder

- Owns requirements/specifications, work allocation, independent review, acceptance criteria, durable status, and engineering phase gates.

### Founder

- Owns market/vertical, customer, pricing, spending, deployment, access, legal, and final business decisions.

## Immediate next gate

The PostgreSQL gate is complete. The next decisions are product and integration gates:

1. Review Dev1's isolated repository-audit hardening separately.
2. Integrate the focused Dev2 commits and this documentation update through reviewed Git history.
3. Secure or confirm a credible design-partner commitment and select the first deterministic engine.
4. AI cofounder finalizes its implementation specification and acceptance traceability.
5. Founder authorizes the selected Phase C/D implementation priority.
6. Assign non-overlapping implementation work to Dev1 and, only when stable ports exist, Dev3.

Phase C code has not started. Green CI makes it technically eligible for authorization; customer/vertical selection and an approved bounded specification remain required.

## Explicit non-claims

- No production PostgreSQL validation.
- No production voice/provider validation.
- No native-language quality approval.
- No clinical approval.
- No load/soak validation.
- No real-call pilot evidence.
- No validated unit economics or provider capacity limits.
