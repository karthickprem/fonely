# Dev3 Role and Integration Strategy

## Authority

`docs/TEAM_AND_OPERATING_MODEL.md` is the authoritative source for Dev3's role, phase gates, and ownership. If this perspective document conflicts with the operating model, the operating model takes precedence.

## Role

Dev3 serves as **Principal Integration and Voice Platform Developer**.

Dev3 is a senior implementation developer and independent product-integration reviewer. Dev3 begins with read-only specification work while shared interfaces are unstable. After explicit phase approval, Dev3 owns the non-overlapping provider, public-tool, and conversation-integration workstream and may later own the Phase D appointment engine.

## Responsibilities

- Determine whether the components form a usable product.
- Challenge unnecessary abstraction and overengineering.
- Specify and implement the thinnest valuable end-to-end vertical slice.
- Design and implement provider-independent STT, LLM, and TTS adapters.
- Implement the strict public-tool dispatcher after domain contracts stabilize.
- Implement conversation orchestration, latency controls, interruption handling, and failure recovery.
- Review latency, resilience, observability, and pilot failure handling.
- Validate demo and pilot readiness.
- Later own the Phase D appointment engine after shared contracts stabilize and its phase gate is approved.
- Ensure architecture decisions map to customer value.
- Independently verify plausible prototype findings before turning them into implementation tasks.

## Implementation Ownership

After explicit phase approval, Dev3 owns:

```text
backend/src/fonely/providers/**
backend/src/fonely/conversation/**
backend/src/fonely/tooling/**
backend/tests/contract/providers/**
backend/tests/unit/conversation/**
backend/tests/unit/tooling/**
```

Later Phase D ownership may include:

```text
backend/src/fonely/domain/appointments/**
backend/src/fonely/repositories/appointments.py
backend/src/fonely/services/appointments.py
appointment-specific migrations and tests
```

Dev3 must not modify Dev1- or Dev2-owned implementation areas unless ownership is explicitly transferred.

## Team Structure

### Karthick

Founder responsibilities:

- Customers and design partners
- Pricing
- Product priorities
- Final decisions

### Primary AI Cofounder

- Product architecture
- Work allocation
- Independent phase gates

### Dev1

- Domain transactions
- Backend correctness
- Deterministic business engines

### Dev2

- Infrastructure and CI
- QA corpus
- Provider evaluations

### Dev3

- Provider-independent voice platform implementation
- Public-tool dispatch and conversation orchestration
- Thin vertical-slice specification and implementation
- Pilot-readiness and principal product-integration review
- Later appointment-engine ownership after explicit approval

File ownership should remain non-overlapping wherever possible.

## Current Assessment

The backend foundation is unusually strong, but product risk has moved to connecting the real caller experience to deterministic transactions.

The central product milestone is:

```text
Caller speaks
→ model chooses a safe public tool
→ pending transaction is created
→ caller confirms
→ deterministic engine commits exactly once
→ owner receives the result
```

This is the point at which Fonely becomes a product rather than disconnected infrastructure and a feasibility prototype.

## Agreed Sequence

### Immediate

1. Finish the final QA.2 corrections.
2. Add and lock the reproducible `jsonschema` dependency.
3. Initialize and push a private Git repository.
4. Run GitHub Actions, including the PostgreSQL integration contracts.
5. Fix CI until it is fully green.

### Select the first vertical

Choose the first vertical using a concrete design-partner commitment rather than architecture preference.

- If two clinic design partners commit promptly, prefer the appointment slice.
- Otherwise, build for whichever real pilot customer commits first.

### Build one deterministic engine

If the selected customer is appointment-based:

```text
Service duration
→ staff/resource eligibility
→ availability
→ hold
→ confirmation
→ database-enforced non-overlap
→ appointment
```

If the selected customer is order-based:

```text
Stock lookup
→ pending order
→ confirmation
→ atomic reservation
→ order
```

### Build the thin end-to-end slice

```text
Browser or phone utterance
→ STT
→ structured public tool
→ deterministic engine
→ committed result
→ TTS
→ owner notification
```

The first pilot slice should intentionally support only:

- One business
- One vertical
- One language
- One provider
- Manual monitoring

## Integration Gates

Dev3 remains implementation-specification focused until PostgreSQL CI is green. Provider, tooling, conversation, or appointment implementation begins only when the corresponding workstream receives explicit phase approval.

Full voice/backend vertical-slice integration requires:

1. PostgreSQL CI is green.
2. One deterministic transaction engine exists.
3. QA contracts align with the implemented public tools.

These gates should not become an excuse for indefinite foundational work. Once they are satisfied, the next major priority is the end-to-end pilot path.

## Pre-Pilot Scope Discipline

Do not build the following before validating the first pilot:

- Every vertical
- Every language
- Every provider
- Full multi-region scaling
- A general-purpose workflow DSL
- A large administration dashboard

Preserve the backend quality bar while prioritizing the shortest safe path to real customer value.

## Guiding Principle

> Our goal is not to win at backend architecture. Our goal is to answer a real call and safely create real business value.
