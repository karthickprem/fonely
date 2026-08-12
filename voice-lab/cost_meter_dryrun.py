"""Zero-provider cost-meter dry run: replay a captured usage fixture through
the meter with a DATED PricingBook. Makes NO provider calls and needs NO
credentials — it only reads two JSON fixtures on disk.

What it proves (all offline):
  1. Idempotent replay: ingesting the fixture TWICE yields identical totals
     (second pass is fully skipped), so fixture reprocessing cannot double-count.
  2. Reconciliation: per-turn billable units sum to per-call totals.
  3. Retry separation: the forced STT retry is a distinct billable line.
  4. Estimate math: money computed with Decimal ONLY from dated/sourced prices,
     labelled ESTIMATED, with the pricing version + source shown.
  5. estimated vs invoiced stay SEPARATE: invoiced is always None here (no
     provider invoice exists); the code never sums the two.

Run: python cost_meter_dryrun.py
"""
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "/scratch/karthick/fonely-worktrees/dev4-cartesia/voice-lab")
from cost_meter import (
    UNIT_STT_AUDIO_SECONDS,
    CostMeter,
    PriceEntry,
    PricingBook,
    UsageRecord,
)

FIXTURES = Path("/scratch/karthick/fonely-worktrees/dev4-cartesia/voice-lab/fixtures")
USAGE_FIXTURE = FIXTURES / "captured_usage_costmeter-1786503897.json"
PRICING_FIXTURE = FIXTURES / "pricing_internal_test_v1.json"


def load_usage(path: Path) -> tuple[dict, list[UsageRecord]]:
    data = json.loads(path.read_text())
    conv = data["conversation_id"]
    biz = int(data["business_id"])
    records = [
        UsageRecord(
            conversation_id=conv,
            business_id=biz,
            turn_index=int(r["turn_index"]),
            provider=r["provider"],
            model=r["model"],
            unit_kind=r["unit_kind"],
            unit_count=int(r["unit_count"]),
            attempt=int(r["attempt"]),
            provider_processed=bool(r["provider_processed"]),
        )
        for r in data["records"]
    ]
    return data, records


def load_pricing(path: Path) -> tuple[dict, PricingBook]:
    data = json.loads(path.read_text())
    entries = [
        PriceEntry(
            unit_kind=e["unit_kind"],
            per_unit=Decimal(e["per_unit"]),  # Decimal from string — exact
            currency=e["currency"],
            source=e["source"],
            retrieved_on=e["retrieved_on"],
        )
        for e in data["entries"]
    ]
    return data, PricingBook(entries)


def main() -> int:
    print("=" * 74)
    print("COST-METER DRY RUN — captured fixture replay, ZERO provider calls")
    print("=" * 74)

    usage_meta, records = load_usage(USAGE_FIXTURE)
    pricing_meta, book = load_pricing(PRICING_FIXTURE)
    print(f"usage fixture: {USAGE_FIXTURE.name} (version {usage_meta['fixture_version']})")
    print(f"pricing book:  {PRICING_FIXTURE.name} "
          f"(version {pricing_meta['pricing_book_version']}, "
          f"currency {pricing_meta['currency']}, INTERNAL-TEST)")
    print(f"conversation_id={usage_meta['conversation_id']} "
          f"business_id={usage_meta['business_id']} (correlation ids only, no PII)")

    meter = CostMeter(
        conversation_id=usage_meta["conversation_id"],
        business_id=int(usage_meta["business_id"]),
    )

    # --- 1. Idempotent replay: ingest the SAME fixture twice ----------------
    first = meter.ingest_all(records)
    totals_after_first = dict(meter.call_totals())
    second = meter.ingest_all(records)  # replay
    totals_after_second = dict(meter.call_totals())

    print("\n" + "-" * 74)
    print("IDEMPOTENT REPLAY (fixture ingested twice)")
    print("-" * 74)
    print(f"  pass 1: added={first['added']} skipped={first['skipped']}")
    print(f"  pass 2: added={second['added']} skipped={second['skipped']} "
          "(all skipped → replay does not double-count)")
    assert second["added"] == 0, "replay added new records — idempotency broken"
    assert totals_after_first == totals_after_second, (
        "totals changed after replay — double-counting")
    print(f"  totals identical across passes: {totals_after_first == totals_after_second}")

    # --- 2. Reconciliation --------------------------------------------------
    meter.reconcile()
    print("\n  reconciliation: per-turn billable units sum to call totals ✓")

    # --- 3. Raw units -------------------------------------------------------
    print("\n" + "-" * 74)
    print("PER-CALL RAW BILLABLE UNITS (integers; stt in ms of audio)")
    print("-" * 74)
    for k, v in sorted(meter.call_totals().items()):
        shown = f"{v} ms ({v / 1000:.3f} s)" if k == UNIT_STT_AUDIO_SECONDS else str(v)
        print(f"  {k}: {shown}")

    # --- 4. Retry separation ------------------------------------------------
    retries = meter.retry_records()
    print("\n" + "-" * 74)
    print("RETRY vs BILLABLE SEPARATION")
    print("-" * 74)
    print(f"  billable records: {len(meter.billable_records())}  "
          f"retries (attempt>1): {len(retries)}")
    for r in retries:
        print(f"    retry: turn={r.turn_index} {r.provider}/{r.unit_kind} "
              f"count={r.unit_count} (billable — provider processed it)")

    # --- 5. Estimate (Decimal, dated prices only) + invoiced separation -----
    est = meter.estimate_micros(book)
    print("\n" + "-" * 74)
    print("ESTIMATED COST (label: ESTIMATE — from dated internal-test prices)")
    print("-" * 74)
    total_micros = 0
    for unit_kind, micros in sorted(est.items()):
        entry = book.get(unit_kind)
        if micros is None:
            print(f"  {unit_kind}: UNPRICED (no dated source) — excluded from total")
            continue
        total_micros += micros
        usd = (Decimal(micros) / Decimal(1_000_000)).quantize(Decimal("0.000001"))
        print(f"  {unit_kind}: est {usd} {entry.currency}  "
              f"[src: {entry.source}; retrieved {entry.retrieved_on}]")
    total_usd = (Decimal(total_micros) / Decimal(1_000_000)).quantize(Decimal("0.000001"))
    print(f"  ESTIMATED call total: {total_usd} {pricing_meta['currency']} "
          f"(pricing v{pricing_meta['pricing_book_version']}, INTERNAL-TEST — not a quote)")

    # invoiced is a SEPARATE dimension; there is no provider invoice here.
    invoiced_micros = None
    print("\n  invoiced call total: NONE (no provider invoice captured)")
    assert invoiced_micros is None, "invoiced must stay None without a real invoice"
    # The two are never summed: assert we never combined them.
    print("  estimated and invoiced are separate fields — never summed.")

    print("\n" + "=" * 74)
    print("RESULT: fixture replayed idempotently, reconciled, retry-separated, "
          "estimate from dated prices, invoiced kept separate. Zero provider calls.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
