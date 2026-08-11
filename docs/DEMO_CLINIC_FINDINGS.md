# What configuring a real clinic exposed

Configuring the demo dental clinic through the mounted internal API, on
PostgreSQL 16.10 at migration head 0015, and then booking against it.
Reproduce with `scripts/seed-demo-clinic.py` followed by
`scripts/prove-booking.py`.

## What works, proven by execution

The onboarding engine is real. A draft goes intake → pending_review →
approved → activated, and activation materialises genuine configuration:
6 services, 2 resources, 9 service-resource eligibilities, 11 operating
schedule rows. Split shifts survive — Monday is stored as two periods,
09:30–13:00 and 17:00–20:30, not flattened into one long opening.

The booking path is real too. Against that configuration:

| Check | Result |
| --- | --- |
| 15:00 Monday, inside the afternoon closure | refused, `outside_operating_hours` |
| 10:00 Monday, inside opening hours | proposed with exact facts |
| Confirmation | appointment committed |
| Retry with the same idempotency key | still exactly one appointment |
| Second patient, same doctor, same time | refused, `capacity_conflict` |

The facts read back on proposal are the ones an agent must say out loud:
service name, doctor name, 15 minutes, price 300, `Asia/Kolkata`. Nothing
is inferred at confirmation time.

Two guarantees deserve specific credit because they held under a real
attempt to break them. `uq_schedule_business_scope` on
`(business_id, day_of_week, open_time)` rejected a duplicate opening at the
database rather than trusting application code. And when activation failed,
the transaction rolled back with no partial write and recorded rollback
evidence — the clinic was left exactly as it was.

## Four defects the run exposed

None are fixed here; onboarding and notifications are not this lane's files.

### 1. A new clinic cannot be created at all

Every onboarding route calls `_get_business_id(request)`, which requires a
positive `X-Business-ID` header and returns 400 without one
(`api/internal/onboarding.py`). No mounted route creates a business. So the
first step of the product — an owner messages us and we set their clinic up
— has no supported path. `seed-demo-clinic.py --bootstrap` inserts the
business and owner directly and labels that step unsupported, because there
is nothing honest to call instead.

### 2. An owner can never change the clinic's hours

`upsert_schedule` and `upsert_exception` are named for an upsert but are
plain inserts with no conflict handling (`repositories/onboarding.py:124`).
Activating a second draft therefore tries to insert an opening that already
exists, and `uq_schedule_business_scope` rejects it. Activation fails, the
draft is stranded in `approved`, and no retry can ever succeed.

The database behaved correctly. The application cannot express "these are
the hours now" — only "add these hours" — so the ordinary case the product
is built around, an owner telling the agent his Saturday timing changed, is
impossible. Replacing the tenant's schedule rows within the same
transaction is the fix.

Observed: first activation `schedules_count=11`; second activation
`duplicate key value violates unique constraint "uq_schedule_business_scope"
DETAIL: Key (business_id, day_of_week, open_time)=(1, 0, 09:30:00)`.

### 3. A failed activation reports no reason

`OnboardingService.activate_configuration` populates `ActivationResult.error`,
and `ActivationResponse` declares an `error` field, but the route constructs
the response with only `success` and `commit_id`
(`api/internal/onboarding.py:236`). The caller receives
`{"success": false, "error": null}` for every failure. The real cause exists
only in `business_configuration_commits.rollback_evidence`, which no API
exposes. An operator watching an owner's activation fail has nothing to act
on.

### 4. A fully onboarded clinic cannot take a single booking

Confirmation calls `NotificationService.create_appointment_notifications`,
which resolves a WhatsApp phone number id for the tenant and raises
`NotificationConfigurationError` when there is none
(`services/notifications.py:170`). That mapping comes from the
`WHATSAPP_BUSINESS_MAPPINGS` environment variable, not from the database,
and onboarding does not create it.

So activation reports success with 6 services and 2 resources, and then
every booking fails. Three things make it worse than a missing setting:

- The failure surfaces as **HTTP 500** at the moment a patient confirms,
  not at activation when the gap first exists.
- The log line carries no cause. `_safe_log_error` puts `error_type` in
  `extra`, which the default formatter drops, so the operator sees only
  `operation_failed`. Diagnosing this required reproducing it in-process.
- Because the mapping is environment configuration, **onboarding a second
  clinic requires an env change and a process restart**. Tenant onboarding
  is not self-service, which is a scaling constraint, not a bug to patch
  late.

Failing closed rather than confirming an appointment nobody is told about is
the right instinct. The check belongs at activation, where it is a
configuration error someone can fix, rather than at the patient's booking,
where it is a lost customer.

## Standing caveat

All of the above is PostgreSQL-tested on a single local instance. None of it
is staging-validated, and no real phone call has been placed. The demo
clinic and its doctors are fictional and must be replaced with a signed
design partner's real configuration before the run means anything to an
investor.
