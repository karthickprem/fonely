# Appointment Engine -- Acceptance Criteria

This document defines the exact acceptance criteria for the Fonely appointment
scheduling engine. Every criterion listed here must be covered by at least one
automated test (unit, integration, or end-to-end).

---

## Service Duration

- Every service has a `duration_minutes` column (integer, NOT NULL).
- Constraint: `duration_minutes > 0`. A value of 0 or negative must be rejected
  at the database level.
- The duration is defined by the business owner on a per-service basis and may
  differ across services within the same business.

---

## Buffers

- Each service may optionally define `buffer_before` and `buffer_after` (integer
  minutes, default 0).
- Both values must be >= 0.
- The **effective reservation window** for a booking is:

  ```
  effective_start = start_at - buffer_before
  effective_end   = start_at + duration_minutes + buffer_after
  ```

- The overlap check (see below) uses the effective reservation window, not the
  bare service duration.

---

## Resource Schedules

- Availability is defined by `OperatingSchedule` rows, which may be scoped to a
  specific resource or to the business as a whole (resource_id NULL = business
  default).
- Each `OperatingSchedule` row specifies a day-of-week, a start time, and an
  end time.
- A resource inherits the business-level schedule unless it has its own
  `OperatingSchedule` rows for that day.
- `ScheduleException` rows override or block availability for a specific date
  or date range (see "Owner Overrides" below).

---

## Split Shifts

- A resource or business may have multiple `OperatingSchedule` rows for the
  same day-of-week to represent split shifts (e.g., 09:00-13:00 and
  17:00-21:00).
- Appointments must fall entirely within one contiguous shift; an appointment
  cannot span the gap between shifts.

---

## Non-Overlap (Exclusion Constraint)

- Appointment time ranges are half-open intervals: `[start_at, end_at)`.
  The start instant is inclusive; the end instant is exclusive.
- The overlap condition for two appointments on the **same resource** is:

  ```
  a.start_at < b.end_at AND b.start_at < a.end_at
  ```

- This is enforced at the database level using a PostgreSQL exclusion
  constraint:

  ```sql
  EXCLUDE USING gist (
      resource_id WITH =,
      tstzrange(effective_start_at, effective_end_at) WITH &&
  )
  WHERE (status NOT IN ('cancelled'))
  ```

- Cancelled appointments are excluded from the overlap check so their time
  slots become available again.

---

## Different Resources at the Same Time

- Two different resources (e.g., Doctor A and Doctor B) may have overlapping
  appointments at the same time.
- The exclusion constraint is scoped to `resource_id WITH =`, so it only
  prevents overlap within a single resource.
- The availability query must return all resources that have an open slot, not
  just the first one found.

---

## Concurrent Booking (Race Condition)

- When two callers attempt to confirm the same slot on the same resource at
  the same time, **exactly one must succeed**.
- The loser must receive a `resource_unavailable` error and must not see a
  partially committed booking.
- This is guaranteed by the exclusion constraint and transaction isolation:
  - The first transaction to commit acquires the slot.
  - The second transaction fails on the exclusion constraint and is rolled
    back.
- The integration test for this scenario must use two concurrent database
  connections with controlled commit ordering.

---

## Holds and Expiry

- A booking may be created in `held` status with a `held_until` timestamp.
- While held, the time slot is reserved (the exclusion constraint applies to
  held bookings).
- A background worker runs periodically to expire stale holds:
  - Any booking where `status = 'held'` and `held_until < now()` is
    transitioned to `expired`.
  - Expired bookings are treated like cancellations: their time slots become
    available.
- The hold duration is configurable per business.

---

## Cancellation

- A booking in `held` or `confirmed` status may be cancelled.
- Cancellation sets `status = 'cancelled'` and `cancelled_at = now()`.
- The cancelled booking's time slot is immediately released (the exclusion
  constraint's WHERE clause excludes cancelled rows).
- A booking that is already `cancelled`, `completed`, or `expired` cannot be
  cancelled again.

---

## Rescheduling

- Rescheduling is implemented as **cancel old + book new**, executed
  atomically within a single database transaction.
- If the new slot is unavailable (exclusion constraint violation), the entire
  transaction is rolled back and the original booking remains in its previous
  status.
- The caller receives either a successful reschedule (with the new booking ID)
  or an error; never a state where the old booking is cancelled but the new
  one does not exist.

---

## Time Zones

- All timestamps are stored as `timestamptz` (timestamp with time zone) in
  PostgreSQL.
- The business has a `timezone` column (e.g., `Asia/Kolkata`).
- When displaying times to the caller or business owner, the application
  converts from UTC to the business timezone.
- Schedule definitions (OperatingSchedule) use local times relative to the
  business timezone. The application converts these to UTC/timestamptz when
  evaluating availability.
- Daylight saving transitions (where applicable) are handled by PostgreSQL's
  timezone support; the application must not perform manual offset arithmetic.

---

## Owner Overrides (ScheduleException)

- A `ScheduleException` row can:
  - **Block** a date or date range: the resource is unavailable regardless of
    the regular OperatingSchedule (e.g., public holiday, personal leave).
  - **Modify** hours for a specific date: the exception provides alternative
    start/end times that replace the regular schedule for that date.
- Exceptions take precedence over the regular OperatingSchedule.
- When evaluating availability for a date, the system checks for exceptions
  first; if none exist, it falls back to the OperatingSchedule.
- Existing confirmed bookings that fall within a newly created blocking
  exception are **not** automatically cancelled. The business owner is
  responsible for handling conflicts (the system should surface a warning).
