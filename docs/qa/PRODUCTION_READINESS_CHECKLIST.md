# Production Readiness Checklist

Staged readiness criteria for Fonely. Each stage must be fully satisfied before
advancing to the next. Items are grouped by category within each stage.

> **Current snapshot:** GitHub Actions run `30687004089` is green: frozen dependency installation, QA/static/migration gates, 281 non-PostgreSQL tests, all 23 PostgreSQL contracts, migration downgrade, and migration re-upgrade passed. This establishes the backend foundation CI gate only. No provider, telephony, load, soak, pilot, or production readiness is established.

---

## Stage 1 -- Developer-Ready

The codebase is safe for any contributor to clone, run tests, and iterate.

### CI/Testing

- [ ] CI pipeline runs on every push (lint, type-check, unit tests, integration tests).
- [ ] All unit tests pass.
- [ ] All integration tests pass against a local PostgreSQL instance.
- [ ] Test coverage is measured and reported (no enforced threshold yet).

### Migrations/Data

- [ ] Alembic migrations apply cleanly on a fresh database (`alembic upgrade head`).
- [ ] Migration parity test (`test_migration_parity.py`) confirms ORM models match the latest migration.
- [ ] No hand-edited migration files with unreviewed SQL.

### Secrets

- [ ] `.env` is listed in `.gitignore` (root and backend).
- [ ] No API keys, tokens, or credentials committed to the repository.
- [ ] `.env.example` documents every required variable with placeholder values.

### Observability

- [ ] Application logs to stdout in structured format.
- [ ] Debug mode is off by default (`debug: bool = False` in config).

### SLOs

- Not applicable at this stage.

### Rate Limits/Quotas

- Not applicable at this stage.

### Abuse Controls

- Not applicable at this stage.

### Load Tests

- Not applicable at this stage.

### Soak Tests

- Not applicable at this stage.

### Failover

- Not applicable at this stage.

### Incident Response

- Not applicable at this stage.

### Privacy/Retention

- [ ] No PII logged in debug output when debug mode is off.

### Telephony/Legal Review

- Not applicable at this stage.

### Backups/Restore

- Not applicable at this stage.

---

## Stage 2 -- Pilot-Ready (5-10 Businesses)

The system can serve a small number of real businesses with manual oversight.

### CI/Testing

- [ ] CI includes integration tests against PostgreSQL (not just SQLite).
- [ ] At least one end-to-end test covering the full call lifecycle (STT, LLM tool call, TTS response).
- [ ] Payload validation tests cover all envelope types (order, stock update, appointment when added).

### Migrations/Data

- [ ] PostgreSQL is the sole production database engine.
- [ ] Migrations tested on a staging PostgreSQL instance before production.
- [ ] Seed data scripts exist for demo/test businesses.

### Secrets

- [ ] Secrets managed via environment variables or a secrets manager (not config files).
- [ ] Exotel API credentials rotated from initial dev values.
- [ ] Sarvam API key scoped to production account.

### Observability

- [ ] Basic monitoring: process uptime, HTTP error rate, database connection count.
- [ ] Alerting configured for: process crash, database unreachable, provider API 5xx rate above threshold.
- [ ] Call outcome distribution logged per business (ordered, booked, enquiry, dropped, escalated).

### SLOs

- [ ] Informal targets documented: P95 response latency target, call completion rate target.
- [ ] Latency measured at the application layer (time from STT result to TTS start).

### Rate Limits/Quotas

- [ ] Sarvam API quota monitored (free tier limits known and tracked).
- [ ] Exotel concurrent call limit documented.

### Abuse Controls

- [ ] Not yet enforced. Manually monitor for unexpected call volume spikes.

### Load Tests

- [ ] Manual smoke test: 3 simultaneous test calls through the full pipeline.

### Soak Tests

- [ ] Not yet required. Monitor stability over pilot usage.

### Failover

- [ ] Exotel integration verified: inbound call routing, audio streaming, call teardown.
- [ ] WhatsApp onboarding flow verified end-to-end for at least one business.

### Incident Response

- [ ] Incident playbook drafted: who to contact, how to disable a business, how to restart services.
- [ ] On-call rotation not needed; single developer reachable during pilot hours.

### Privacy/Retention

- [ ] Call transcripts stored with business_id scoping (no cross-tenant leakage).
- [ ] Customer phone numbers stored in E.164 format only.
- [ ] Retention policy not yet enforced but documented as a future requirement.

### Telephony/Legal Review

- [ ] At least one language verified end-to-end (Tamil recommended for initial pilot).
- [ ] Exotel terms of service reviewed for compliance with AI-answered calls.

### Backups/Restore

- [ ] Database backup mechanism tested (pg_dump or provider snapshot).
- [ ] Restore tested at least once on a staging instance.

---

## Stage 3 -- Early Production (50-100 Businesses)

The system handles moderate load with defined quality targets.

### CI/Testing

- [ ] Test suite runs in under 5 minutes.
- [ ] Integration tests cover all PendingAction state transitions.
- [ ] Negative-path tests: expired actions, stale versions, unauthorized access.

### Migrations/Data

- [ ] Migration rollback tested for the most recent migration.
- [ ] Schema changes go through a review checklist (locking impact, index strategy).

### Secrets

- [ ] API key rotation procedure documented and tested.
- [ ] Database credentials rotated on a schedule.

### Observability

- [ ] Structured logging with correlation IDs (call_id or session_id in every log line).
- [ ] Dashboard: call volume, order/appointment counts, error rates, latency percentiles.
- [ ] Provider-specific metrics: STT accuracy signals, TTS latency, LLM token usage.

### SLOs

- [ ] P95 response latency < 2 seconds (from caller utterance end to AI speech start).
- [ ] Valid-call completion rate > 90% (calls where the customer gets a definitive answer).
- [ ] Correct intent recognition > 90%.
- [ ] Wrong transaction rate < 2%.

### Rate Limits/Quotas

- [ ] Per-business rate limits on API requests (prevent runaway integrations).
- [ ] LLM token budget per call enforced (prevent prompt injection cost attacks).

### Abuse Controls

- [ ] Repeated rapid-fire calls from the same number throttled.
- [ ] Maximum pending actions per session enforced.

### Load Tests

- [ ] API endpoint load test: sustained 50 requests/second for 10 minutes.
- [ ] Concurrent call simulation: 10 simultaneous calls through the full pipeline.
- [ ] Transaction contention test: 5 concurrent reservations for the same inventory item.

### Soak Tests

- [ ] 8-hour soak test with steady call volume. No memory leaks, no connection exhaustion.

### Failover

- [ ] Provider latency injection: system degrades gracefully when STT/TTS/LLM responds slowly (> 5s).
- [ ] Database connection recovery after transient failure.

### Incident Response

- [ ] Runbook covers: high error rate, provider outage, database failover, call quality degradation.
- [ ] Incident severity levels defined (P0-P3 with response time targets).

### Privacy/Retention

- [ ] Privacy policy published and accessible.
- [ ] Call transcript retention window defined (e.g., 90 days).
- [ ] Customer data deletion process documented.

### Telephony/Legal Review

- [ ] Multi-language beta: at least 3 languages tested with real callers.
- [ ] Cost per connected minute tracked per business.

### Backups/Restore

- [ ] Automated daily backups with off-site storage.
- [ ] Point-in-time recovery tested.
- [ ] Backup monitoring: alert if backup older than 24 hours.

---

## Stage 4 -- Growth (1,000 Businesses)

The system is production-hardened for significant scale.

### CI/Testing

- [ ] Performance regression tests in CI (latency benchmarks for critical paths).
- [ ] Chaos testing: random process restarts during active calls.
- [ ] Contract tests for provider API integrations (Sarvam, Exotel).

### Migrations/Data

- [ ] Online schema migration strategy (no downtime migrations).
- [ ] Large table migrations tested with realistic data volumes.
- [ ] PostgreSQL exclusion constraint for appointment overlap prevention deployed and verified.

### Secrets

- [ ] Secrets manager integration (AWS Secrets Manager, HashiCorp Vault, or equivalent).
- [ ] No long-lived credentials; short-lived tokens where providers support them.

### Observability

- [ ] Distributed tracing across call lifecycle (STT, LLM, domain logic, TTS).
- [ ] Business-level health dashboard (per-business call success rate, order accuracy).
- [ ] Anomaly detection on call volume and error patterns.

### SLOs

- [ ] Formal SLO definitions with error budgets.
- [ ] P95 response latency < 2 seconds.
- [ ] Call completion rate > 90%.
- [ ] System availability > 99.5% (measured monthly).
- [ ] SLO burn-rate alerting.

### Rate Limits/Quotas

- [ ] Tiered rate limits by subscription plan.
- [ ] Provider quota headroom monitored (alert at 80% of Sarvam/Exotel limits).

### Abuse Controls

- [ ] Automated detection of anomalous call patterns (toll fraud, prompt injection attempts).
- [ ] Business suspension workflow for policy violations.
- [ ] Customer block list per business.

### Load Tests

- [ ] Peak load test: 50-80 simultaneous calls sustained for 30 minutes.
- [ ] Database write contention: 20 concurrent order commits for overlapping inventory.
- [ ] Appointment hot-slot race: 10 concurrent bookings for the same time slot.

### Soak Tests

- [ ] 48-hour soak test at expected daily load profile.
- [ ] Memory and connection pool stability verified over soak period.

### Failover

- [ ] Provider failover tested: simulate Sarvam 503 and verify graceful degradation.
- [ ] Database failover tested: promote replica, verify zero data loss.
- [ ] Worker restart during active call: verify call recovery or clean teardown.

### Incident Response

- [ ] Dedicated on-call rotation (at least 2 engineers).
- [ ] Post-incident review process with published findings.
- [ ] Automated incident detection and paging.

### Privacy/Retention

- [ ] Data retention automation: transcripts and PII purged on schedule.
- [ ] Tenant data isolation audit completed.
- [ ] GDPR-style data export and deletion endpoints.

### Telephony/Legal Review

- [ ] Regulatory review: TRAI compliance for AI-answered calls.
- [ ] Caller disclosure: AI identifies itself as an AI assistant at call start.
- [ ] Terms of service and privacy policy reviewed by legal counsel.

### Backups/Restore

- [ ] Backup restore drill: full restore completed in under 1 hour.
- [ ] Cross-region backup replication.
- [ ] Backup encryption verified.

---

## Stage 5 -- Scale (Thousands of Businesses)

The system is resilient, redundant, and ready for sustained high-volume operation.

### CI/Testing

- [ ] Full regression suite runs on every PR with parallelized test execution.
- [ ] Canary deployment with automated rollback on error rate increase.
- [ ] Synthetic monitoring: automated test calls every 15 minutes.

### Migrations/Data

- [ ] Database read replicas for query-heavy paths (call history, reporting).
- [ ] Table partitioning evaluated for high-volume tables (calls, inventory_movements).
- [ ] Data archival strategy for records older than retention window.

### Secrets

- [ ] Automated credential rotation with zero-downtime rollover.

### Observability

- [ ] Multi-region observability with centralized log aggregation.
- [ ] Real-time cost tracking per business, per provider.
- [ ] Capacity forecasting based on growth trends.

### SLOs

- [ ] SLA commitments to paying customers (published uptime guarantees).
- [ ] SLO dashboards visible to engineering and business teams.
- [ ] Error budget policies: feature freeze when budget exhausted.

### Rate Limits/Quotas

- [ ] Dynamic rate limiting based on system load.
- [ ] Per-business concurrency limits aligned with subscription tier.

### Abuse Controls

- [ ] Machine-learning-based anomaly detection on call patterns.
- [ ] Automated business onboarding fraud detection.

### Load Tests

- [ ] Peak load test: 200-500 simultaneous calls sustained for 1 hour.
- [ ] Cross-region failover load test.
- [ ] Provider capacity limits measured and documented.

### Soak Tests

- [ ] Weekly 48-hour soak test as part of release qualification.
- [ ] Long-term stability tracking (connection pool, file descriptor, memory trends).

### Failover

- [ ] Multi-region deployment consideration documented and evaluated.
- [ ] Provider redundancy: secondary STT/TTS/LLM provider tested as fallback.
- [ ] Database read replica promotion tested with automated failover.
- [ ] CDN for static assets (onboarding media, hold music, prompt audio).

### Incident Response

- [ ] Dedicated on-call with escalation tiers and SLA-driven response times.
- [ ] Incident communication plan for customer-facing outages.
- [ ] Quarterly incident simulation drills.

### Privacy/Retention

- [ ] Compliance audit completed (data protection, telecom regulations).
- [ ] Annual privacy review cycle established.
- [ ] Third-party security assessment.

### Telephony/Legal Review

- [ ] TRAI compliance verified and documented.
- [ ] Multi-state/multi-regulation review for pan-India operation.
- [ ] Insurance coverage for service disruptions evaluated.

### Backups/Restore

- [ ] Automated backup verification (restore and validate integrity weekly).
- [ ] Disaster recovery plan with documented RTO and RPO targets.
- [ ] Multi-region backup with independent restore capability.
