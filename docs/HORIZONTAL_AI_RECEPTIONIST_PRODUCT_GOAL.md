# Fonely Horizontal AI Receptionist Product Goal

**Status:** Founder-mandated product and architecture constraint  
**Recorded:** 2026-08-11

## Product goal

Fonely is a multilingual AI receptionist platform for businesses across Tamil Nadu first and India later. Independent dental clinics are the first pilot and commercial beachhead; they are not the permanent boundary of the product.

The platform must ultimately support:

1. **Multiple customer businesses simultaneously** with strict tenant isolation, independent configuration, usage limits, evidence, and operations.
2. **Multiple kinds of businesses** that need an AI receptionist, such as dental clinics, salons, wellness providers, diagnostic centres, home-service companies, automotive workshops, and professional-service offices.
3. **Multiple channels and languages** over the same authoritative business capabilities, beginning with Tamil-first voice and expanding through WhatsApp and other justified channels and Indian languages.

Phase 0 remains intentionally narrow: prove a world-class, safe, reliable, commercially valuable Tamil appointment-booking experience for one dental clinic. Focused launch scope must not become dental-specific core architecture.

## Architecture constraint

The reusable core must remain based on generic concepts and capabilities:

- Business and location
- Customer and actor identity
- Service and price
- Resource and service-resource eligibility
- Operating schedule and exceptions
- Availability, appointment, cancellation, and rescheduling
- Conversation and pending confirmed action
- Notification, escalation, and immutable evidence
- Tenant-scoped commands, repositories, and transactions
- Provider-neutral voice, messaging, model, and telephony ports
- Canonical business onboarding and configuration

Dental-specific terminology, medical-safety restrictions, escalation wording, required onboarding fields, prompts, and evaluation scenarios must be expressed through bounded vertical configuration and policy. They must not be hard-coded into the generic conversation engine, authoritative booking transaction, or shared database model.

A new vertical should be addable primarily through:

1. Business configuration
2. A bounded vertical policy module
3. Onboarding fields and validation
4. Conversation vocabulary and presentation
5. Safety and escalation rules
6. A vertical evaluation corpus

The deterministic core should change only when a new market reveals a genuinely reusable business capability.

## Multi-tenant production guarantees

Supporting multiple businesses means more than storing a `business_id`. The platform must preserve these guarantees as it grows:

- Every tenant-owned read and write is scoped by trusted `business_id`.
- Caller- or model-supplied tenant identity is never authoritative.
- Provider channel identifiers map to tenants through trusted configuration.
- One business cannot observe, mutate, infer, or receive another business's customers, schedules, prices, conversations, appointments, notifications, usage, or evidence.
- Per-business configuration, policies, language preferences, provider routing, quotas, retention, and operational controls are independently enforceable.
- Retry, idempotency, concurrency, and transaction guarantees remain correct across tenants and replicas.
- Metrics and support tools permit tenant-level diagnosis without exposing customer PII.

Tenant isolation requires PostgreSQL constraints and indexes, typed domain/application policy, repository scoping, adversarial tests, and production observability. Naming a tenant column alone is not sufficient evidence.

## Scalability posture

Fonely will use a modular monolith first. The current FastAPI, PostgreSQL, SQLAlchemy, durable inbox/outbox, bounded worker, and provider-adapter approach is suitable for the initial product and practical horizontal growth.

Scale first through:

- Stateless API and channel-adapter replicas
- Bounded worker replicas and durable leases
- PostgreSQL connection pooling and measured query/index tuning
- Tenant-aware rate limits, concurrency limits, and usage quotas
- Provider timeout, retry, circuit-breaker, and failover policies
- Managed operational services where they reduce small-team risk
- Centralized metrics, redacted logs, tracing, alerts, and cost reporting

Do not introduce microservices, multi-region architecture, workflow DSLs, or speculative platform abstractions without measured operational need. Technology is considered suitable when it is maintainable, secure, observable, economical, replaceable at external-provider boundaries, and proven under expected load—not merely because it is new.

## Commercial constraint

Horizontal capability does not justify a broad initial launch. Fonely must win one repeatable customer problem before expanding.

For each vertical, validate:

- A painful and frequent missed-enquiry or reception problem
- A complete customer outcome, not only a conversation
- Willingness to pay and a sustainable acquisition path
- Cost per successful outcome and gross margin
- Language/channel quality with real users
- Safety, privacy, and human-escalation requirements
- Onboarding and support effort that a small team can sustain

Expansion to another vertical should occur only after the dental pilot supplies evidence about customer value, reliability, operations, retention, and unit economics, unless the founder explicitly changes the beachhead.

## Decision test

For every significant feature or architecture proposal, classify it as one of:

1. **Generic receptionist core** — reusable across markets.
2. **Shared deterministic business capability** — reusable by a meaningful set of verticals.
3. **Vertical policy/configuration** — dental or another market's bounded behavior.
4. **Channel/provider adapter** — transport only, without authoritative business rules.
5. **Speculative scope** — not required by a validated customer problem and therefore deferred.

The Phase 0 release may be dental-specific in configuration and customer experience. It must remain horizontal in its trusted core, tenant model, provider boundaries, and growth path.

## Evidence standard

This document describes the intended product and architecture. It does not prove that the current implementation satisfies the goal.

Claims that Fonely supports multiple businesses or verticals require progressively stronger evidence:

1. Architecture and policy design
2. Implemented tenant and configuration boundaries
3. Unit and adversarial tenant-isolation tests
4. Real PostgreSQL concurrency and isolation tests
5. Exact-SHA CI evidence
6. Integrated multi-business staging evidence
7. Load, soak, failure, backup, restore, and operational evidence
8. Controlled pilots in the relevant verticals

Until those gates run, describe the platform as designed or implemented for horizontal use—not validated at scale.
