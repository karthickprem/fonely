# Inventory Engine -- Acceptance Criteria

This document defines the exact acceptance criteria for the future Fonely inventory and order engine. Phase C is not implemented or authorized; existing inventory/order tables are foundation schema, not proof of transactional behavior. Every criterion listed here must be covered by at least one automated test (unit, PostgreSQL integration, or end-to-end) before inventory/order functionality is pilot-enabled.

---

## Atomic Reservation

- When a caller places an order, stock is reserved using `SELECT ... FOR UPDATE`
  on the `InventoryBalance` row for the product.
- The reservation checks `available_qty >= requested_qty` where
  `available_qty = on_hand_qty - reserved_qty`.
- If sufficient stock is available, `reserved_qty` is incremented and an
  `InventoryReservation` row is created in a single transaction.
- If insufficient stock is available, the transaction is rolled back and the
  caller receives an `insufficient_stock` error.

---

## Negative-Stock Prevention

- The `InventoryBalance` table has the following CHECK constraints:
  - `ck_inv_on_hand`: `on_hand_qty >= 0`. On-hand stock can never go negative.
  - `ck_inv_reserved_lte_on_hand`: `reserved_qty <= on_hand_qty`. Reserved
    quantity can never exceed on-hand quantity.
- These constraints are enforced at the database level and act as a safety net
  behind the application-level checks.
- Any attempt to violate these constraints (via direct SQL, a bug, or a race
  condition) must result in a transaction rollback.

---

## Multi-Item Rollback

- When an order contains multiple line items, all reservations are performed
  within a single database transaction.
- If any product in the order fails reservation (insufficient stock), **all**
  reservations for that order are rolled back.
- The caller receives a single error indicating which product(s) could not be
  reserved.
- No partial orders are persisted. The system never leaves an order in a state
  where some items are reserved and others are not.

---

## Walk-In Sales

- The business owner can record a walk-in sale (a sale that was not initiated
  through a phone call or online order).
- A walk-in sale deducts `on_hand_qty` directly without going through the
  reservation flow.
- The deduction is recorded as an `InventoryMovement` with
  `movement_type = 'walk_in_sale'`.
- The `ck_inv_on_hand` constraint prevents overselling even for walk-ins.

---

## Expiry and Cancellation Release

- When an order is cancelled or an `InventoryReservation` expires:
  - `reserved_qty` on the `InventoryBalance` is decremented by the reserved
    amount.
  - The reservation status is set to `released`.
  - An `InventoryMovement` is recorded with the appropriate movement type
    (`cancellation_release` or `expiry_release`).
- The released stock becomes immediately available for other orders.

---

## Price Snapshots

- When an order is confirmed, each `OrderLineItem` stores
  `price_per_unit_snapshot` -- the price at the time of confirmation.
- Subsequent changes to the product's current price do not affect existing
  confirmed orders.
- The total order amount is computed from the snapshots, not from the current
  product price.
- This ensures that the business and the caller agree on the price that was
  communicated during the call.

---

## Quantity Precision

- Quantities are stored as `NUMERIC(10,2)` -- at most 2 decimal places, up to
  10 total digits.
- Values with more than 2 decimal places (e.g., `0.001`) must be rejected by
  the application before reaching the database.
- Validation occurs at the API/domain layer with a clear error message
  indicating the precision constraint.

---

## Idempotency

- The following unique constraints prevent duplicate processing:
  - `uq_order_idempotency`: prevents creating the same order twice (keyed on
    the client-supplied idempotency key).
  - `uq_inv_res_idempotency`: prevents reserving stock twice for the same
    order line item.
- When a duplicate request arrives (same idempotency key), the system returns
  the existing record instead of creating a new one.
- This protects against network retries, double-clicks, and conversation-layer
  replays.

---

## Ledger Consistency

- Every stock change (reservation, release, walk-in sale, restock, adjustment)
  is recorded as an `InventoryMovement` row with a signed `delta` value.
- The following invariant must hold at all times:

  ```
  SUM(InventoryMovement.delta) WHERE product_id = X
    == InventoryBalance.on_hand_qty WHERE product_id = X
  ```

- A periodic consistency check (and/or a database trigger) verifies this
  invariant and raises an alert if it is violated.
- `InventoryMovement` rows are append-only; they are never updated or deleted.

---

## Concurrent Final-Stock Race

- When two callers attempt to order the last available stock of a product
  simultaneously:
  - Exactly one caller succeeds and receives a confirmed reservation.
  - The other caller receives an `insufficient_stock` error.
  - No stock goes negative; no phantom reservations exist.
- This is guaranteed by the `SELECT ... FOR UPDATE` locking strategy combined
  with the CHECK constraints.
- The integration test for this scenario must use two concurrent database
  connections with controlled commit ordering to deterministically exercise the
  race condition.
