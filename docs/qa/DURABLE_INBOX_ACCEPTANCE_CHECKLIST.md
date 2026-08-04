# Durable WhatsApp Inbox — Production Acceptance Checklist

Task: `DEV2-DURABLE-INBOX-FINAL-CORRECTION-20260804-01`

| # | Invariant / blocker | Production files | Required tests / evidence |
|---|---|---|---|
| 1 | Domain, PendingAction, appointment, allocations, evidence, notifications, response outbox, conversation turn and inbound state are one commit | `services/conversation.py`, `services/appointments.py`, `workers/inbound_worker.py` | PG failure injection for booking/cancel/reschedule response enqueue failure; zero surviving state |
| 2 | No provider request while DB row/advisory lock or checked-out transaction is held | `workers/inbound_worker.py`, provider planning boundary | Provider instrumentation test asserting no active transaction during call |
| 3 | Lock key stable across processes | `repositories/inbound_events.py` | subprocesses with different `PYTHONHASHSEED` produce identical key |
| 4 | Strict per-conversation order, failed head blocks later event | `repositories/inbound_events.py` | two-session A then “yes” test; failed/backoff A blocks B; different conversations claim concurrently |
| 5 | Durable claim ownership and leases | migration `0014`, ORM, repository | A lease expires; B reclaims/succeeds; stale A transition rejected |
| 6 | Failure bookkeeping does not read expired ORM state | `workers/inbound_worker.py` | real AsyncSession failure after claim persists failed state without `MissingGreenlet` |
| 7 | Cache state restored/invalidated on rollback | `services/conversation.py`, `workers/inbound_worker.py` | enqueue failure rolls DB back, cache invalidated, retry reloads prior committed state |
| 8 | One logical outbound row and visible ambiguous delivery | notification outbox/sender/worker | provider accepted + commit failure leaves durable attempt evidence; no second logical row |
| 9 | Originating trusted phone number selects sender | inbound/outbound identity + sender resolver | two tenants/two numbers route correctly; cross-tenant identity rejected |
| 10 | Missing credentials fail closed | `run_worker.py`, compose | startup configuration tests; pending events unchanged |
| 11 | Inbound completion follows response terminal policy | notification worker + inbound repository | delivered response -> completed/body NULL; permanent failure -> response_failed/dead-letter |
| 12 | Terminal domain failure queues one truthful fallback | inbound worker | max attempts queues fallback exactly once; no outcome claim |
| 13 | Owner authorization uncertainty retries | `_is_owner` / worker | DB error does not route owner as patient |
| 14 | Polling transient DB failures recover | worker loop | session/claim/idle commit/failure bookkeeping injection, later event processed |
| 15 | Webhook authentication mandatory outside explicit dev mode | webhook/app startup | missing secret/token/mapping fails closed; unsigned payload never persisted |
| 16 | Strict bounded webhook parsing | webhook validator | null/malformed nested fields; count limits; chunked oversized body |
| 17 | Every tenant-owned transition is business + event + status + claim scoped | inbound repository | cross-tenant and stale-claim updates affect zero rows / raise typed error |
| 18 | Dead-letter plaintext retention bounded <=30 days | migration, retention service, `DATA_INVENTORY.md` | PG cleanup test and policy assertions |
| 19 | One dedup source only | migration `0014`, ORM | ORM parity; live upgrade/downgrade/re-upgrade |
| 20 | PostgreSQL enforces status, attempts, terminal timestamps/body, phone and claim consistency | migration `0013/0014`, ORM | invalid live INSERT/UPDATE tests |
| 21 | Migration values/head/preflight/cycle are exact | migration/parity/readiness | exact permitted enum values; exact head `0014`; populated downgrade preflight; live cycle |
| 22 | Acceptance is real PostgreSQL evidence, not mocks | `tests/integration/postgres/test_inbound_durable_postgres.py` | all listed independent-session/concurrency/rollback tests execute |
| 23 | Deployable worker topology | Dockerfile/compose/entrypoints | image-context tests, required env, worker health/restart, all worker services configured |

No item is complete until its named PostgreSQL/deployment evidence executes successfully.
