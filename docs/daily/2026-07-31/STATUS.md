# Fonely Daily Status — 2026-07-31

## Executive summary

Fonely progressed from a browser-based voice prototype into the beginning of a production Python backend. The product scope is now a multilingual AI business assistant for Indian MSMEs supporting both:

- Appointment workflows: clinics, salons, tutors, and other scheduled services.
- Inventory/order workflows: meat shops, bakeries, flower shops, and other finite-stock businesses.
- Hybrid businesses that enable both capabilities.

The current production backend has a credible Phase A foundation, but Phase A is **not approved as complete yet**. Static checks and 71 unit tests pass, and migration `0001` now contains 18 application tables. A final correction pass is pending for ORM/migration parity, database enum enforcement, quantity precision, and stronger migration verification.

---

## 1. Product decisions made

### Product interface

- Business-owner interface: WhatsApp.
- Customer interface: normal phone call.
- No browser UI or admin dashboard in the MVP.
- The earlier browser page remains only a prototype/testing surface.

### Product capabilities

- One capability-based platform, not separate hardcoded applications per vertical.
- A business can enable inventory/orders, appointments, or both.
- Business category is a template hint, not the behavior authority.
- LLM handles natural-language understanding and response generation.
- Deterministic code and PostgreSQL are authoritative for stock, prices, totals, schedules, orders, and appointments.
- All transactions require structured validation and explicit confirmation.

### Language strategy

- Canonical Fonely locales are separated from provider-specific codes.
- Planned languages include Tamil, Hindi, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi, Odia, Assamese, Urdu, and Indian English.
- Romanized regional languages and natural code-switching are part of the product requirement.
- Language availability and language quality status are separate: experimental, beta, or verified.

### Pricing direction

Verified public competitor benchmarks generally cluster around ₹5–₹9 per connected minute.

Current launch hypothesis, not final pricing:

- Pilot: ₹999/month including approximately 75 connected minutes.
- Overage: approximately ₹7.99/connected minute.
- Longer-term usage corridor: ₹6.99–₹7.99/minute plus number/platform fee and GST.

Final pricing remains gated on measured Exotel, Sarvam, WhatsApp, and infrastructure costs.

---

## 2. Prototype work completed

### Browser voice demo

A prototype demonstrated:

- Tamil/Tanglish customer interaction.
- Sarvam speech-to-text, Sarvam-105B conversation, and Bulbul v3 text-to-speech.
- A sample dental appointment conversation.
- Natural language follow-up questions and spoken responses.

### Prototype limitations discovered

The demo is not production behavior:

- It conversationally claimed an appointment without a database transaction.
- It gave medical guidance that should instead be safely escalated to a clinician.
- Browser TTS latency and phone-quality audio have not been validated through Exotel.
- The prototype code is not the production backend.

### External integrations

- Sarvam API access was tested previously.
- Exotel account and trial ExoPhone were created.
- Exotel AgentStream/Voicebot production enablement remains external and unverified.
- WhatsApp Business API is not integrated.

### Security action

Previously shared Sarvam and Exotel credentials must be considered exposed and rotated. Replacement secrets must be entered directly into local configuration or a secrets manager, never into chat or documentation.

---

## 3. Production backend built

Location:

```text
/scratch/karthick/fonely/backend
```

### Runtime and project tooling

✅ Python 3.12 installed under scratch using `uv`.

✅ `pyproject.toml` defines:

- Hatchling build backend.
- `src/` package layout.
- FastAPI, SQLAlchemy async, asyncpg, Alembic, Pydantic, HTTPX, and WebSockets dependencies.
- Development dependencies for pytest, Ruff, mypy, coverage, and factories.
- Strict mypy configuration.
- Ruff lint and formatting configuration.

✅ Editable package import works without manually setting `PYTHONPATH`.

### Configuration and database foundation

✅ Pydantic Settings configuration exists.

✅ Async SQLAlchemy engine/session setup exists for PostgreSQL.

✅ `.env.example` contains placeholders only.

✅ `.gitignore` excludes `.env`, virtual environments, caches, coverage data, and local database files.

⚠️ This directory is not currently a Git repository, so “tracked-file secret scan” cannot yet be claimed.

### Domain enums

Implemented enums include:

- Capabilities.
- Locale roles and validation status.
- Subscription status.
- Pending-action type/status.
- Order and appointment status.
- Inventory reservation and movement status/type.
- Product units.
- Call outcomes and caller roles.
- Business authorization roles: owner and manager only.

### Value validation

Implemented and unit-tested value boundaries include:

- Indian mobile normalization for owner/manager WhatsApp identities.
- Generic E.164 validation for caller/telephony identities.
- Canonical Fonely locale allowlist.
- IANA timezone validation.
- Timezone-aware datetime validation.
- Decimal-only money and quantity inputs; Python float inputs are rejected.
- Nonnegative and positive decimal values.
- INR amount precision capped at two decimal places.
- INR half-up quantization utility.
- Canonical Odia `or-IN` mapped to provider-specific Sarvam `od-IN`.

### Database schema

Migration/ORM currently model 18 application tables:

1. `businesses`
2. `business_capabilities`
3. `business_locales`
4. `business_users`
5. `operating_schedules`
6. `schedule_exceptions`
7. `products`
8. `services`
9. `resources`
10. `pending_actions`
11. `calls`
12. `inventory_balances`
13. `orders`
14. `order_line_items`
15. `inventory_reservations`
16. `inventory_movements`
17. `appointments`
18. `owner_audit_log`

Important schema design now present:

- Capability-based businesses.
- Multiple locales per business.
- BusinessUser as planned owner/manager authorization authority.
- Split weekly schedules and per-date exceptions.
- Products, services, and appointment resources.
- Daily inventory balances with reserved quantities.
- Per-product reservation records with expiry.
- Append-only inventory movement concept.
- Normalized order line items and price snapshots.
- Tenant-scoped idempotency for pending actions, orders, appointments, and reservations.
- Pending-action persistence for confirmation lifecycle.
- Appointment resource lookup index.
- Audit records for owner-initiated changes.

### Alembic migration

✅ Migration revision exists:

```text
0001 — initial_schema
```

✅ Offline upgrade SQL renders 19 `CREATE TABLE` statements:

- 18 Fonely application tables.
- 1 `alembic_version` table.

✅ Migration source contains 18 `op.create_table` operations and 18 `op.drop_table` operations.

🟡 Migration has not been applied to a real PostgreSQL database.

🟡 Real upgrade/downgrade execution and `alembic check` remain pending.

---

## 4. Verification completed

The following results were independently observed after the latest Phase A implementation:

```text
ruff check .                     PASS
ruff format --check .            PASS
mypy src                         PASS
pytest -m "not postgres" -q      71 passed
alembic heads                    0001
alembic offline upgrade SQL      19 CREATE TABLE statements
migration source operations      18 create_table / 18 drop_table
```

Test coverage currently includes:

- Indian mobile validation.
- E.164 validation.
- Locale acceptance/rejection.
- IANA timezone validation.
- INR amount validation and float rejection.
- Quantity positivity/nonnegativity.
- Aware datetimes.
- INR quantization.
- Expected schema table presence.
- Selected constraints and idempotency keys.
- Enum membership.
- Locale-to-Sarvam mapping.
- Basic migration source-content checks.

### Not yet verified

- Migration against a live PostgreSQL database.
- Database enum rejection behavior.
- Concurrent pending-action transition behavior.
- Concurrent inventory reservation.
- Appointment overlap prevention.
- API routes.
- Sarvam integration in the production backend.
- Exotel phone-call streaming.
- WhatsApp onboarding/management.
- Razorpay payment.

---

## 5. Known Phase A defects still pending

These issues were found after the 71 tests passed and must be fixed before Phase B begins.

### P0 — ORM/migration parity

1. `Call.transcript` nullability differs:
   - ORM currently infers non-null.
   - Migration permits null.

2. `OwnerAuditLog.details` nullability differs:
   - ORM currently infers non-null.
   - Migration permits null.

3. Enum string lengths differ:
   - `inventory_movements.movement_type`: ORM and migration lengths differ.
   - `pending_actions.status`: ORM and migration lengths differ.

### P0 — Database enum enforcement

The ORM/migration currently use string columns for enum-backed values without proven database-level enum/check enforcement. Invalid raw status strings could be persisted.

Required resolution:

- Use `native_enum=False`, `create_constraint=True`, and `validate_strings=True` consistently, or explicit named check constraints.
- Keep migration and ORM identical.

### P0 — Unnecessary extension

Migration `0001` creates `btree_gist`, but no exclusion constraint uses it yet. This may require privileges unavailable to the application migration role.

Required resolution:

- Remove it from `0001`.
- Add it only with the future appointment-overlap exclusion migration.

### P1 — Quantity precision

Quantity validators currently allow arbitrary decimal scale while database quantity columns use `NUMERIC(10,2)`.

Risk:

- A positive application value such as `0.001` can become `0.00` in the database.

Required resolution:

- Define and test at-most-two-decimal quantity precision, or choose a finer explicit storage precision.

### P1 — Non-finite Decimal values

NaN and positive/negative infinity require explicit rejection and tests.

### P1 — Documentation corrections

- INR policy is “at most two decimal places,” not “exactly two.”
- `ROUND_HALF_UP` is half-up rounding, not banker’s rounding.

### P1 — Stronger migration tests

Current migration tests mainly inspect source text and selected metadata. They did not detect ORM/migration nullability and type differences.

Required resolution:

- Add parity checks for exact tables, columns, nullability, types, foreign keys, unique constraints, checks, and indexes.
- Render explicit offline downgrade range: `0001:base`.

### P1 — Tenant ownership invariant

Cross-tenant relationships are not fully database-enforced. Every service/repository must query IDs with `business_id`. Composite database keys may be added where practical.

---

## 6. Pending implementation roadmap

### Phase A — Finish foundation

Status: 🟡 In progress

Pending:

- Fix ORM/migration parity.
- Enforce database enum domains.
- Remove premature `btree_gist` extension.
- Add quantity scale and finite-decimal validation.
- Correct validation documentation.
- Strengthen migration parity tests.
- Render and inspect offline downgrade SQL.
- Apply migration and downgrade on PostgreSQL when available.

Exit criteria:

- All complete-project static gates pass.
- Unit tests pass.
- Offline migration parity tests pass.
- No known ORM/migration differences.
- PostgreSQL execution is either verified or explicitly blocked.

### Phase B — Pending-action state machine

Status: ⏳ Not started

Build:

- Strict Pydantic commands/results.
- Versioned action payload envelope.
- Explicit transition graph.
- Create, revise, await confirmation, begin commit, complete/fail commit, reject, cancel, and expire operations.
- Deterministic confirmation snapshots.
- Tenant-scoped idempotency.
- Conditional version updates.
- Owner/manager authorization from BusinessUser.
- Unit tests for all transitions and invalid states.
- PostgreSQL tests for concurrent commit and stale versions.

### Phase C — Inventory/order engine

Status: ⏳ Not started

Build:

- Get current stock.
- Authorized owner stock updates.
- Walk-in sales.
- Create/revise pending order.
- Atomic order confirmation.
- Multi-product row locking in stable order.
- All-or-nothing stock reservation.
- Order cancellation and pickup completion.
- Reservation expiry.
- Inventory movement ledger consistency.
- Idempotency and concurrency tests.

### Phase D — Appointment engine

Status: ⏳ Not started

Build:

- Service/resource availability.
- Resource schedules and exceptions.
- Slot holds and expiry.
- Atomic appointment confirmation.
- PostgreSQL exclusion constraint for overlapping resource intervals.
- Rescheduling/cancellation.
- Parallel-resource tests.

### Phase E — AI tool boundary

Status: ⏳ Not started

Build:

- Strict action schemas.
- Capability- and role-based tool allowlist.
- Provider-independent LLM adapter.
- Validated tool dispatcher.
- Sanitized typed results.
- No direct LLM database access.
- Multilingual intent fixtures and evaluations.

### Phase F — Voice pipeline

Status: ⏳ Production integration not started

Build:

- Sarvam streaming STT.
- Sarvam/Fish Audio provider adapters for TTS evaluation.
- Streaming TTS.
- Exotel AgentStream bidirectional audio.
- Correct telephony codec/resampling.
- Barge-in, silence, interruption, and reconnection handling.
- Per-stage latency and cost telemetry.

### Phase G — WhatsApp owner experience

Status: ⏳ Not started

Build:

- Language-first onboarding.
- Business/capability setup.
- Product/service/resource setup.
- Owner natural-language updates.
- Order/appointment notifications.
- Daily summaries.
- Customer confirmations.

### Phase H — Payments and provisioning

Status: ⏳ Not started

Build:

- Razorpay subscription/credit flow.
- Dedicated number provisioning.
- Usage limits and prepaid overage.
- Subscription enforcement.
- Cost and margin reporting.

### Phase I — Pilot

Status: ⏳ Not started

Plan:

- 10 founding businesses.
- Tamil and English first for quality validation, while architecture supports all locales.
- Measure call completion, order/booking success, latency, cost, and willingness to pay.
- Use pilot results to set final plans.

---

## 7. External blockers

### PostgreSQL

🚫 No local PostgreSQL, Docker, or Podman is currently available on the development machine.

Needed later:

- A local/remote PostgreSQL test instance.
- `FONELY_TEST_DATABASE_URL` for marked integration tests.
- Real migration upgrade/downgrade validation.
- Concurrency tests.

### Exotel

🚫 AgentStream/Voicebot activation and commercial production pricing remain external dependencies.

Need written confirmation of:

- Connected-minute charge.
- Number rental.
- AgentStream surcharge.
- Billing increments.
- Concurrent-call limits.
- GST and volume pricing.

### WhatsApp

🚫 Provider and Business API onboarding are not yet selected/completed.

### Credentials

⚠️ Previously exposed credentials require rotation before any production integration work.

---

## 8. Next-day objective

### Objective for 2026-08-01

> Finish Phase A completely, then stop for review.

Bounded deliverable:

1. Fix ORM/migration nullability and type parity.
2. Add database enum constraints.
3. Remove premature `btree_gist` creation.
4. Add finite and precision-safe quantity validation.
5. Strengthen migration parity tests.
6. Render upgrade and `0001:base` downgrade SQL.
7. Run complete-project quality gates.
8. Do not begin Phase B until review approves Phase A.

---

## 9. Files of interest

```text
fonely/
├── docs/
│   ├── PLAN.md
│   └── daily/
│       ├── README.md
│       └── 2026-07-31/
│           └── STATUS.md
└── backend/
    ├── pyproject.toml
    ├── alembic.ini
    ├── migrations/
    │   ├── env.py
    │   └── versions/0001_initial_schema.py
    ├── src/fonely/
    │   ├── core/
    │   │   ├── config.py
    │   │   ├── database.py
    │   │   ├── exceptions.py
    │   │   ├── locale_mapping.py
    │   │   └── validators.py
    │   └── models/
    │       ├── enums.py
    │       └── schema.py
    └── tests/
        ├── conftest.py
        ├── test_schema.py
        └── test_validators.py
```

---

## 10. Definition of done for daily reporting

For future logs, use these labels precisely:

- **Implemented:** source code exists.
- **Statically verified:** lint/type checks pass.
- **Unit tested:** deterministic tests execute and pass.
- **PostgreSQL tested:** migrations/transactions run against PostgreSQL.
- **Provider tested:** real Sarvam/Exotel/WhatsApp contract exercised.
- **Pilot validated:** real business/customer use confirms value and reliability.

As of 2026-07-31, Fonely's production backend is **implemented and unit-tested at the Phase A foundation level**, with known final Phase A corrections pending. It is not yet transaction-complete, telephony-integrated, or production validated.
