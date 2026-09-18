# ADR 0001 — Accepting Exotel Status Callbacks Without Provider Authentication

- **Status:** PROPOSED — requires founder acceptance before any production exposure
- **Date:** 2026-08-10
- **Owner:** CEO (risk acceptance), Dev2 (gateway/ops controls), Dev1 (application controls)
- **Acceptance owner:** Karthick — not yet accepted
- **Reviewed by:** Chief Architect — approved as proposed, subject to the amendments incorporated here
- **Scope:** Controlled pilot only. Not accepted for general production.

## Context

Fonely receives call status callbacks from Exotel at `/webhooks/exotel/call-status`.
These callbacks drive call state in our system.

Exotel does **not** cryptographically authenticate its status callbacks. Verified
against Exotel's published authentication documentation on 2026-08-10: there is no
HMAC, no signature header, and no documented callback authentication mechanism.
Exotel's only recommended verification is **source IP allowlisting over HTTPS**.

This is a property of the provider, not a gap in our implementation. We cannot
verify a signature that is never sent.

At time of writing the route is unmounted — `exotel.py` mounts only when
`EXOTEL_WEBHOOK_SECRET` is configured — and no production exposure has occurred.

## Decision

Accept Exotel status callbacks for a **controlled pilot only**, protected by
compensating controls rather than provider authentication.

This is sufficient for pilot **only if all of the following hold**. If any
prerequisite fails, the route must be **absent**, not present-and-rejecting:

1. Source IP allowlisting against **verified, current** Exotel CIDRs.
2. TLS in transit.
3. Gateway-injected high-entropy secret; any client-supplied internal auth header
   is **stripped** before the gateway injects its own.
4. Proxy chain is exclusive and non-spoofable; direct application access is
   **network-blocked**, not merely discouraged.
5. Authentication occurs **before** body parse and before any database access.
6. Bounded JSON and multipart body sizes; rate limits applied.
7. Duplicates ACK only after durable commit.
8. Provider-independent correlation (below).

The same gateway, proxy, and IP controls apply to the **media/start path**. That
path is not itself provider-authenticated, so it earns trust from gateway admission
— not from Exotel.

### Provider-independent correlation

IP allowlisting alone makes our security posture dependent on Exotel's network
hygiene. We add a control that does not.

Correlate each callback against a **gateway-admitted media/start record, or a
trusted outbound provisioning record**, binding
`(provider environment/account, CallSid, called Fonely number, business, direction)`
within a short validity window.

**Outcomes are explicit:**

| Outcome | Condition | Action |
|---|---|---|
| **Matched** | All bound fields agree within the validity window | Eligible for call-state application |
| **Pending** | No record yet (status preceded media/start) | Quarantine as `unverified_pending_correlation`; no domain mutation, no customer-facing effect |
| **Conflict** | Wrong business, number, account, or direction | Dead-letter and raise a security alert. **Never eligible for later auto-match.** |

- **Inbound calls must NOT be rejected merely for being unknown.** Genuine inbound
  patient calls are not pre-originated; rejecting unknown inbound CallSids would
  break the primary product path.
- **Outbound calls** require an originated-call record or nonce.
- **Quarantine rows are written only AFTER gateway authentication.** Otherwise the
  quarantine table is itself an unauthenticated write target and can be flooded.
- **Expiry:** pending records expire to manual review after a configured duration.
  Duration and its owner live in secure configuration, not here.
- **Manual review cannot mutate domain state** except through an audited operator
  command. Reviewing is not committing.

### Correlation is not a substitute for existing guarantees

Correlation sits alongside, and does not replace:

- semantic idempotency and deduplication,
- provider event identity,
- forward-only transition validation,
- late and out-of-order event handling.

A replayed but correctly-correlated event must still be rejected by dedup.

### Verification

Acceptance is proven by adversarial probe, not by the presence of auth code:

- A **forged `X-Forwarded-For`** request produces zero application inbox rows, zero
  quarantine rows, and zero database effect.
- A **direct application** request (bypassing the gateway) produces the same: zero.
- A **gateway-authenticated fixture** succeeds.

Gateway edge logs may record rejected attempts, PII-safe. "Zero effect" refers to
application state, not to edge observability.

Because IP is the only provider-side control, forged-XFF is the *primary* attack
case, not a secondary one.

## Risk accepted

**An attacker who can source traffic from an allowlisted IP, or who can influence
apparent source IP, can submit forged call status events.** Correlation bounds the
blast radius to calls that genuinely exist, but does not eliminate the exposure.

Assumptions this rests on:

- Exotel's published CIDRs are accurate, current, and not shared with untrusted tenants.
- Our proxy chain cannot be influenced by a client.
- Exotel's own infrastructure is not compromised.

## Monitoring

Alert on:

- callbacks from non-allowlisted sources (should be zero),
- direct-application access attempts,
- spoofed forwarded-header rejections,
- rate-limit anomalies,
- quarantine **age**, not only growth — a slowly-ageing pending set is a failure that
  volume metrics miss,
- correlation conflicts and dead letters,
- **the route being mounted while any prerequisite is false.**

## Privacy

- No raw callback bodies, phone numbers, or headers in logs.
- Quarantine retention and the manual-review SLA are defined in the runbook and
  bounded — quarantined records are not kept indefinitely.

## Incident response

1. Unmount or disable the route — do not rely on secret revocation alone. IP is the
   primary authentication, so rotating the injected secret does not close the path.
2. Block at the gateway edge.
3. Preserve sanitized evidence for investigation.

## Revalidation

- Re-verify Exotel CIDRs against the authoritative source at least quarterly, and
  before any expansion beyond pilot.
- Re-open this ADR if Exotel changes callback behaviour.

## Exit criteria

This ADR is superseded when any of the following becomes true:

- Exotel supports signed callbacks.
- We move to a provider that authenticates callbacks.
- A stronger gateway-side mechanism (e.g. mutual TLS with the provider) is available.

## Founder dependency

Karthick must supply and accept:

1. The **authoritative source** for Exotel CIDRs and support confirmation of them —
   a citable source with a date, not merely a list of IP values.
2. **Explicit acceptance of the residual pilot risk** described above.

Item 2 is a business risk decision and is not delegable to engineering.

## Notes

Operational values — actual CIDRs, secrets, hostnames, expiry durations — are
deliberately **not** recorded here. They live in secure configuration. This document
records the decision and its rationale only.

**Link from** (to be wired when these files exist): deployment runbook
(`docs/STAGING_DEPLOYMENT.md` covers staging today; a production runbook does not yet
exist), and the readiness checklist.
