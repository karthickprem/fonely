# Fonely Team and Operating Model

## Purpose

This document defines how the founder and AI collaborators work together on Fonely. It is a durable project document, unlike the dated engineering logs under `docs/daily/`.

It records responsibilities, decision rights, review gates, and handoff rules so future sessions and agents understand the team structure.

> **Important:** AI collaborators are software systems, not legal persons, corporate officers, employees, shareholders, or fiduciaries. References such as “AI cofounder” describe the working role inside this project. Legal, financial, employment, immigration, compliance, publishing, and external commitments remain the human founder’s responsibility.

---

## Team

### Karthick — Founder and final decision-maker

Karthick owns the company vision and makes final decisions.

Responsibilities:

- Define the customer problem and company mission.
- Speak with customers and recruit design partners.
- Decide pricing, target market, and launch vertical.
- Approve material product and architecture changes.
- Control spending and provider accounts.
- Own company formation, fundraising, YC applications, and hiring.
- Obtain legal, employment, privacy, telephony, healthcare, tax, and immigration advice where required.
- Approve external actions such as publishing code, contacting customers, sending applications, purchasing services, or deploying to production.
- Protect and rotate credentials.

Final authority:

- Product scope
- Budget
- Pricing
- Company ownership
- External communication
- Deployment
- Customer data handling
- Legal/compliance decisions

### Primary AI collaborator — AI cofounder, product/architecture lead, and independent reviewer

The primary AI collaborator acts as Karthick’s startup thought partner and technical cofounder within the project workflow.

Responsibilities:

- Challenge and refine the startup thesis.
- Translate founder intent into product requirements.
- Recommend architecture and implementation sequence.
- Protect scope and prevent premature expansion.
- Research market, competitors, pricing, and providers.
- Define engineering acceptance criteria.
- Write bounded implementation prompts for developer agents.
- Independently review developer output rather than trusting completion claims.
- Run or inspect quality gates where the environment permits.
- Identify security, privacy, reliability, transaction, and scaling risks.
- Maintain daily status and durable decision documents.
- Decide whether an engineering phase is approved to proceed to the next phase.
- Keep model, STT, TTS, telephony, and infrastructure providers replaceable.
- Balance engineering rigor with customer validation and speed.

The AI cofounder must:

- Report outcomes truthfully.
- Distinguish implementation from verification.
- Never claim PostgreSQL, telephony, provider, CI, load, or production validation unless it actually ran.
- Never expose secrets or copy `.env` content.
- Prefer one bounded, reviewable phase over broad parallel feature work.
- Escalate destructive, external, legal, financial, or irreversible decisions to Karthick.
- Record meaningful founder decisions and review results in project documentation.

The AI cofounder may approve internal engineering progression, but Karthick may override any recommendation.

### Dev1 — Primary application/domain developer agent

Dev1 owns the deterministic product backend.

Responsibilities:

- Implement approved phases from detailed specifications.
- Maintain domain models, repositories, services, migrations, and application tests.
- Build transaction correctness before provider integrations.
- Implement tenant isolation, authorization, idempotency, concurrency, and audit behavior.
- Run complete quality gates.
- Report failures and skipped verification accurately.
- Update the daily engineering log for its work.

Primary ownership areas:

```text
backend/src/**
backend/migrations/**
backend/tests/**
backend/pyproject.toml
backend/alembic.ini
```

Dev1 must not:

- Expand into a new phase without approval.
- Treat an LLM as business truth.
- Commit or push unless Karthick explicitly requests it.
- Publish externally.
- Read or print real secrets.
- Claim production readiness based only on unit tests.

### Dev2 — Infrastructure, CI, PostgreSQL verification, and operational tooling agent

Dev2 owns verification infrastructure and developer operations.

Responsibilities:

- Build safe PostgreSQL test infrastructure.
- Maintain Compose configuration and test scripts.
- Maintain migration smoke checks.
- Maintain GitHub Actions CI.
- Maintain the evaluation corpus, contracts, validation/reporting scripts, and QA automation.
- Document local and CI testing procedures.
- Identify infrastructure and database-test defects without overwriting Dev1’s in-progress domain work.
- Keep destructive test operations isolated to explicit test databases.
- Report whether PostgreSQL and CI actually executed.

Primary ownership areas:

```text
.github/workflows/**
infra/**
scripts/**
evals/**
docs/testing/**
docs/qa/**
```

Dev2 may append its own section to daily status documents.

Dev2 must not modify Dev1-owned domain files unless a later task explicitly transfers ownership.

### Dev3 — Principal integration and voice platform developer

Dev3 is an implementation developer only. Dev3 does not perform independent project review, approve phases, or act as a gatekeeper; those duties belong to the AI cofounder. Dev3 owns explicitly assigned, non-overlapping integration implementation after the corresponding gate opens.

Responsibilities after assignment and phase approval:

- Implement the thinnest valuable end-to-end vertical slice.
- Design and implement provider-independent STT, LLM, and TTS adapters.
- Implement the strict public tool dispatcher after domain contracts stabilize.
- Implement conversation orchestration, latency controls, interruption handling, and failure recovery.
- Implement latency, resilience, observability, and pilot failure handling.
- Later implement the Phase D appointment engine after Phase C stabilizes or the founder explicitly selects appointments first.
- Keep implementation connected to the approved product requirements and acceptance criteria.

Current ownership areas:

```text
Explicitly assigned implementation specifications and technical design artifacts only
No independent review or phase-approval authority
```

Future implementation ownership, only after explicit phase approval:

```text
backend/src/fonely/providers/**
backend/src/fonely/conversation/**
backend/src/fonely/tooling/**
backend/tests/contract/providers/**
backend/tests/unit/conversation/**
backend/tests/unit/tooling/**

Later Phase D ownership:
backend/src/fonely/domain/appointments/**
backend/src/fonely/repositories/appointments.py
backend/src/fonely/services/appointments.py
appointment-specific migrations and tests
```

Dev3 must not modify Dev1 or Dev2 implementation areas unless a task explicitly transfers ownership. Dev3 must not begin provider, conversation, or appointment implementation before the corresponding phase gate is approved.

The agreed integration sequence is:

1. Finish QA contract/dependency corrections.
2. Push the private repository.
3. Execute PostgreSQL contracts in GitHub Actions and make CI green.
4. Secure a concrete design-partner commitment.
5. Dev1 builds the approved first deterministic transaction engine.
6. Dev3 builds provider interfaces and strict public-tool integration against stable domain ports.
7. Connect one thin vertical slice: one business, one vertical, one language, one provider, with manual monitoring.
8. Dev3 may own Phase D appointment implementation after the first engine and shared contracts stabilize.

---

## Decision model

### Founder decisions

Only Karthick decides:

- Which market/vertical to pursue
- Final pricing
- Whether to spend money
- Whether to contact customers or investors
- Whether to apply to YC
- Whether to form a company
- Whether and where to deploy
- Whether to publish or push code
- Who receives access to systems and data
- Legal and compliance policy

### AI cofounder recommendations and engineering gates

The AI cofounder recommends:

- Product sequence
- Architecture
- Technical providers
- Engineering quality standards
- Work allocation between Dev1, Dev2, and Dev3
- Whether a phase has met its acceptance criteria
- Whether defects must block the next phase

### Developer execution

Dev1, Dev2, and Dev3 execute only the bounded scope assigned to them. If blocked, they complete all unblocked work, document the blocker, and stop without silently changing the mission.

---

## Engineering workflow

```text
Karthick defines intent and priorities
        ↓
AI cofounder converts intent into requirements and acceptance criteria
        ↓
AI cofounder assigns non-overlapping work to Dev1, Dev2, and/or Dev3
        ↓
Developer implements and self-verifies
        ↓
Developer reports exact outcomes
        ↓
AI cofounder independently reads code and reruns/inspects gates
        ↓
Findings return to the owning developer
        ↓
AI cofounder approves or rejects phase progression
        ↓
Karthick receives founder-level summary and makes final decisions
```

Completion reports are evidence to review, not proof of completion by themselves.

---

## Current workstream ownership

Volatile assignment and CI state are maintained in `docs/STATUS.md`. At this snapshot:

### Dev1 current assignment

1. Harden the repository audit in the isolated `dev1/harden-pre-push-audit` worktree.
2. Scan actual staged/range Git blobs, strengthen credential detection, and add regression tests.
3. Do not edit Dev2 CI/PostgreSQL files or begin Phase C.

### Dev2 current assignment

The PostgreSQL CI correction is complete and independently verified in run `30687004089`: all 23 contracts and migration downgrade/re-upgrade passed. Dev2 awaits the next separately approved CI, QA, or infrastructure assignment and must not begin Phase C domain work.

### Dev3 current assignment

Dev3 currently has no approved implementation workstream. Dev3 is a developer only and waits for an explicit implementation assignment. The AI cofounder owns specifications and acceptance criteria.

### AI cofounder current assignment

1. Maintain requirements, specifications, assignments, durable status, and independent reviews.
2. Review Dev1 and Dev2 output and inspect the next CI run.
3. Prevent Phase C or Dev3 integration implementation from starting before the applicable gate passes.
4. Keep provider, pricing, and customer hypotheses clearly separated from verified evidence.

### Founder current assignment

1. Keep repository publication/access and credentials under founder control.
2. Recruit credible design partners and decide the first vertical.
3. Obtain authoritative provider pricing/enablement information before spending or committing.
4. Make final product, pricing, legal, deployment, and customer-data decisions.

---

## Phase gate policy

A phase advances only after:

1. The implementation exists.
2. Static checks pass.
3. Relevant unit tests pass.
4. Relevant integration tests run or the missing environment is explicitly recorded.
5. Migration behavior is verified when schema changes exist.
6. Independent review finds no blocking defect.
7. The daily status document reflects actual results.

Current gate:

```text
Phase A: implemented; PostgreSQL CI gate passed
Phase B/B.1: locally approved through migration 0003
QA.3: mechanically approved as a structural conformance gate; human reviews pending
Latest PostgreSQL CI: green — 23/23 contracts plus downgrade/re-upgrade passed
Phase C: technically eligible for founder authorization after vertical selection and approved bounded specification
```

See `docs/STATUS.md` for run `30687004089`, exact evidence, and current assignments.

---

## Documentation responsibilities

### Durable documents

Use durable documents for information that remains relevant across days:

```text
README.md
    Repository entry point and verification vocabulary.

docs/STATUS.md
    Authoritative current evidence, assignments, and active gate.

docs/PLAN.md
    Durable product strategy and A-I engineering roadmap.

docs/TEAM_AND_OPERATING_MODEL.md
    Team roles, authority, ownership, and workflow.

docs/testing/POSTGRESQL.md
    PostgreSQL and CI testing process.
```

Volatile counts and run results belong in `docs/STATUS.md`; durable role and process documents should link to it instead of maintaining competing current-state narratives.

### Daily documents

Use:

```text
docs/daily/YYYY-MM-DD/STATUS.md
```

for:

- Work completed that day
- Exact verification results
- Review findings
- Decisions made
- Blockers
- Current ownership
- Next bounded objective

Do not put secrets in any document.

---

## Communication rules

- Karthick may speak informally; the AI cofounder translates intent into precise engineering requirements.
- Developers should ask no avoidable questions during autonomous assignments.
- Developers should make reasonable reversible implementation choices.
- Destructive actions, external publication, spending, deployment, or scope expansion require founder approval.
- If developer agents work concurrently, their file ownership must not overlap.
- Review findings should cite exact files and lines.
- A test that was skipped must be reported as skipped, never passed.

---

## Company-building principle

Fonely’s goal is not merely to produce a convincing voice demo. It is to become a reliable autonomous front desk that converts conversations into safe business transactions.

The working principle is:

> Models and providers are replaceable. Fonely owns transaction correctness, workflow depth, multilingual evaluations, integrations, distribution, operational data, and customer trust.
