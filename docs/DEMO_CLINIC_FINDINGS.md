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

Three are now fixed and proven by execution; the fourth is fixed in the part
that can be fixed without a migration, and explicitly deferred in the part
that cannot. Status is recorded under each defect. The findings themselves
are left as originally written, so the record shows what was wrong rather
than only what is now right.

### 1. A new clinic cannot be created at all

Every onboarding route calls `_get_business_id(request)`, which requires a
positive `X-Business-ID` header and returns 400 without one
(`api/internal/onboarding.py`). No mounted route creates a business. So the
first step of the product — an owner messages us and we set their clinic up
— has no supported path. `seed-demo-clinic.py --bootstrap` inserts the
business and owner directly and labels that step unsupported, because there
is nothing honest to call instead.

**Fixed.** `POST /internal/v1/businesses` creates the tenant an owner's first
message implies. It is the only internal route that runs without
`X-Business-ID`, since there is no tenant to be scoped to yet. The owner's
phone identifies the clinic, so a repeat send returns the existing tenant
with 200 rather than standing up a second one; a transaction-scoped advisory
lock on the phone makes that hold under concurrent sends, not merely
sequential ones. No migration was needed.

Proven against the running app:

```
1 first send   -> 201 {'business_id': 2, 'owner_user_id': 3, 'created': True}
2 repeat send  -> 200 {'business_id': 2, 'owner_user_id': 3, 'created': False}
3 no auth      -> 401
```

`--bootstrap` is no longer the only path; every proof below runs on business
2, which was created through this route.

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

**Fixed.** Two changes, both inside the caller's transaction and behind the
schedule locks activation already takes, so availability never observes a
clinic with no hours.

`upsert_schedule` and `upsert_exception` are now real upserts. Business-level
and resource-level rows live under two different *partial* unique indexes, so
the conflict target names the same predicate PostgreSQL used to build the
index — `index_where=resource_id IS NULL` against
`(business_id, day_of_week, open_time)`, or `IS NOT NULL` against the
resource-scoped four-column index. Naming the columns alone is not enough;
PostgreSQL will not match a partial index without its predicate.

That alone would still leave a dropped day open forever, because an upsert
only touches rows the draft mentions. So activation first retires the whole
timetable: `deactivate_schedules` flips the tenant's active openings to
inactive, `delete_exceptions` removes its exceptions, and the draft then
restates every opening it still declares. A commit says what the clinic's
hours *are*, not what to add to them. Schedules are deactivated rather than
deleted (availability already filters `is_active`, ids survive, history stays
readable); exceptions are deleted outright, because they carry no `is_active`
column and a withdrawn closure must stop suppressing bookings.

Proven on business 2, created through the route from defect 1 — no
`--bootstrap`:

```
first activation : Saturday 09:30-14:00, schedules_count=11
second activation: Saturday 09:30-15:00, schedules_count=11
OK: re-activation replaced the configuration rather than duplicating it.
```

Row count unchanged and the closing time actually moved: the second
activation replaced the timetable instead of colliding with it.

### 3. A failed activation reports no reason

`OnboardingService.activate_configuration` populates `ActivationResult.error`,
and `ActivationResponse` declares an `error` field, but the route constructs
the response with only `success` and `commit_id`
(`api/internal/onboarding.py:236`). The caller receives
`{"success": false, "error": null}` for every failure. The real cause exists
only in `business_configuration_commits.rollback_evidence`, which no API
exposes. An operator watching an owner's activation fail has nothing to act
on.

**Fixed.** Both `ActivationResponse` constructions in the route now pass
`error=result.error`, so a failed activation carries its cause. This is the
smallest of the four and the least independently exercised: it is covered by
the code path, not by a dedicated failing-activation run.

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

**Partly fixed; the rest is deliberately deferred.**

Fixed now: the failure is no longer opaque. `NotificationConfigurationError`
was caught nowhere, so it fell through to the catch-all and the patient's
confirmation returned `{"detail": "Internal error"}` with the cause only in a
traceback. The confirm route now catches it, rolls back, logs
`confirmation_blocked_by_configuration` with the error code, and returns
**503 naming the cause**. 503 rather than 500 is the honest classification:
the request was well-formed and will succeed once the tenant's channel is
configured. This is a real handler on a live path, not defensive decoration —
the raise propagates out of `confirm_and_commit` uncaught, which was verified
by reading every `except` between the two.

Proven against the running app:

```
1 unmapped clinic (business 2) -> 503 whatsapp_mapping_missing
2 mapped clinic   (business 1) -> 200 {'status': 'committed', 'appointment_id': 8,
                                       'service_name': 'Consultation',
                                       'resource_name': 'Dr. Rajesh Kumar', ...}
```

The second line matters as much as the first. An earlier run of this check
returned 409 `capacity_conflict` on a slot a previous run had taken, and the
script counted that as a pass — the healthy commit path had not executed at
all. The check now walks forward a week at a time until it finds a genuinely
free slot and fails loudly if it cannot, so "never ran" can no longer read as
"worked".

Deferred, and this is the substantive half: the mapping still comes from the
`WHATSAPP_BUSINESS_MAPPINGS` environment variable. Onboarding a second clinic
therefore still requires an env change and a process restart, so tenant
onboarding is still not self-service. The fix is a `business_whatsapp_channels`
table written during activation, which needs a migration. Alembic head is
0015 and **0016 is reserved for Dev1's Exotel work, which already has code
written against it**, so taking that revision here would create a competing
migration. This waits for 0016 to land.

The original recommendation stands and is also not yet done: the check
belongs at activation, where it is a configuration error someone can fix,
rather than at the patient's booking, where it is a lost customer. Moving it
there is only worth doing once the mapping lives in the database, since an
activation-time check against an env var would pass and then rot at the next
restart.

## A fifth defect, found while fixing the fourth

The deployment `.env` is shared with processes that are not this app — it
carries `NODE_ENV` — and pydantic-settings forbids undeclared keys, so
`create_app()` raised a validation error before binding a port. The genuinely
ours keys are now declared (`cartesia_api_key`, `cartesia_voice_id`,
`exotel_trial_number`) and unknown keys are ignored. The tradeoff is stated
in the code: a misspelled Fonely key now takes its default silently instead
of failing loudly.

Clearing that exposed a quieter and worse problem. Router groups in `app.py`
are gated on the credential they need, and that `.env` has no
`INTERNAL_API_SECRET` — so the entire internal API, booking and conversations
and onboarding, did not register. The app booted clean and every booking call
returned 404, which is indistinguishable from a wrong URL. Absence read as
success.

`app.py` now logs each disabled router group at startup together with the
exact setting that enables it. Verified both ways: with the secret set, 16
paths register and only the two genuinely-absent groups warn; without it, the
missing capability announces itself.

## Standing caveat

All of the above is PostgreSQL-tested on a single local instance. None of it
is staging-validated, and no real phone call has been placed. The demo
clinic and its doctors are fictional and must be replaced with a signed
design partner's real configuration before the run means anything to an
investor.
