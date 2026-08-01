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
- Repository-audit hardening is complete and independently approved through commits `3902387`, `35cd179`, and `49f8033`; it is integrated on `integration/green-foundation-audit`.
- Dev1 awaits the next separately approved domain assignment and must not edit Dev3's appointment work.

### Dev2 — developer

- Owns CI, PostgreSQL verification infrastructure, QA tooling/corpus maintenance, and operational test tooling.
- PostgreSQL CI correction is complete and green.
- Pre-D appointment verification traceability is independently approved and integrated: 59 requirements, consistent evidence classifications, 48 future PostgreSQL contracts, and one post-commit adapter contract.
- Dev2 waits for stable Dev3 D1/D2 interfaces before implementing those tests and must not edit Dev3 implementation.

### Dev3 — developer only

- Has no independent review, specification-approval, or phase-gate authority.
- Explicitly assigned Phase D D1/D2 for the generic appointment capability, configured first for the nearby salon.
- Current authorized scope: migration `0004`, appointment-specific ORM/enums, ResourceAllocation capacity design, pure appointment domain, appointment PendingAction payloads, and unit/parity tests.
- D3 repository/service transactions, WhatsApp, voice, providers, and public tooling remain gated.

### AI cofounder

- Owns requirements/specifications, work allocation, independent review, acceptance criteria, durable status, and engineering phase gates.

### Founder

- Owns market/vertical, customer, pricing, spending, deployment, access, legal, and final business decisions.

## Immediate next gate

The founder selected salon appointments, so Phase D is brought forward while Phase C inventory/orders is deferred.

1. Dev1 audit hardening is approved and integrated locally; run CI after publishing/integrating the branch.
2. Dev3 implements only D1/D2 appointment schema/pure-domain work and stops for independent review.
3. The AI cofounder reviews migration `0004`, ResourceAllocation capacity semantics, create-versus-mutation commit linkage, payloads, and tests against the approved Dev2 verification blueprint.
4. Dev2 implements the 48 live PostgreSQL appointment contracts only after Dev3's interfaces stabilize and receives a separate assignment.
5. The post-commit caller-success adapter contract is implemented only when a real transaction runner/tool adapter exists.
6. WhatsApp setup and voice integration remain later gates.

Phase C inventory/order code has not started and is intentionally deferred.

## Explicit non-claims

- No production PostgreSQL validation.
- No production voice/provider validation.
- No native-language quality approval.
- No clinical approval.
- No load/soak validation.
- No real-call pilot evidence.
- No validated unit economics or provider capacity limits.
