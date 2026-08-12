"""Per-turn cost meter for the voice pipeline — RAW measured units.

Design constraints (set with cofounder2, all load-bearing):
  * RAW provider usage units only. STT=audio seconds, LLM=tokens (prompt +
    completion, kept separate), TTS=characters. Integers where the provider
    counts integers; Decimal for any money.
  * Correlation by conversation_id + business_id ONLY. NEVER PII, transcript
    text, or audio bytes in a meter record. (The transcript is measured for
    its LENGTH and then discarded; the text never enters a record.)
  * Retry attempts are SEPARATE records from successful billable units. A
    provider call that the provider actually processed is billable even if the
    turn later fails; a call that never reached the provider is not.
  * estimated vs invoiced are DISTINCT fields and never summed together.
    No API here returns an invoiced amount, so `invoiced_micros` is always
    None — stated, not implied.
  * No provider price is authoritative without a dated source. Money is
    produced ONLY from a PricingBook whose every entry carries a source string
    and a retrieved-on date. Absent a book, the meter emits units and NO money.
  * A reconciliation assertion: per-turn billable units summed per provider
    must equal the per-call totals the meter reports.

What each provider actually returns (verified 2026-08-12 against live APIs):
  - LLM gateway: usage{prompt_tokens, completion_tokens, ...} + dated model id.
  - Sarvam STT: request_id, transcript, language — NO usage. Seconds are
    measured from the WAV locally.
  - Cartesia TTS: audio bytes, NO usage headers. Characters are the input
    transcript length, measured locally.
"""
from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


# --- Units -----------------------------------------------------------------
# Provider billing dimensions, named exactly so a record can never conflate
# tokens with seconds with characters.
UNIT_STT_AUDIO_SECONDS = "stt_audio_seconds"
UNIT_LLM_PROMPT_TOKENS = "llm_prompt_tokens"
UNIT_LLM_COMPLETION_TOKENS = "llm_completion_tokens"
UNIT_TTS_CHARACTERS = "tts_characters"


@dataclass(frozen=True)
class UsageRecord:
    """One measured provider unit for one attempt of one turn.

    Holds NO PII, NO transcript, NO audio — only correlation ids, the provider/
    model, the unit kind, an integer count, the attempt number, and whether the
    provider processed it (billable). request_id is the provider's own handle
    for later reconciliation against an invoice; it carries no caller data.
    """
    conversation_id: str
    business_id: int
    turn_index: int
    provider: str            # "sarvam" | "gateway" | "cartesia"
    model: str               # dated where the provider gives one
    unit_kind: str
    unit_count: int          # integer provider units
    attempt: int             # 1-based; >1 means a retry
    provider_processed: bool # True → billable even if the turn later failed
    request_id: Optional[str] = None

    @property
    def identity(self) -> tuple:
        """Stable idempotency key for one billable usage event. Reprocessing a
        fixture (or replaying an event stream) must not double-count, so this
        key uniquely names the (call, turn, provider, dimension, attempt) event.
        request_id is NOT part of the key: the same logical attempt keeps its
        identity even if a replay lacks the provider handle."""
        return (self.conversation_id, self.turn_index, self.provider,
                self.unit_kind, self.attempt)


@dataclass(frozen=True)
class PriceEntry:
    """A single dated, sourced price. Money is only ever computed from these."""
    unit_kind: str
    per_unit: Decimal        # currency per ONE unit
    currency: str
    source: str              # where this number came from (URL / doc / contract)
    retrieved_on: str        # ISO date the number was read from that source


class PricingBook:
    """A set of dated, sourced prices. No entry, no estimate for that unit —
    the meter will not invent a price."""

    def __init__(self, entries: list[PriceEntry] | None = None) -> None:
        self._by_unit: dict[str, PriceEntry] = {}
        for e in entries or []:
            self._by_unit[e.unit_kind] = e

    def get(self, unit_kind: str) -> Optional[PriceEntry]:
        return self._by_unit.get(unit_kind)

    @property
    def is_empty(self) -> bool:
        return not self._by_unit


def wav_duration_seconds(wav_bytes: bytes) -> Decimal:
    """Exact audio duration from a WAV container: frames / frame-rate. This is
    the STT billable dimension (Sarvam bills audio length, and returns none)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
    if rate == 0:
        return Decimal(0)
    # Keep full precision; round only at money time, never at unit time.
    return (Decimal(frames) / Decimal(rate))


@dataclass
class CostMeter:
    """Accumulates UsageRecords for one call and reconciles them."""
    conversation_id: str
    business_id: int
    records: list[UsageRecord] = field(default_factory=list)

    def record_stt(self, *, turn_index: int, wav_bytes: bytes, model: str,
                   attempt: int, provider_processed: bool,
                   request_id: str | None = None) -> None:
        seconds = wav_duration_seconds(wav_bytes)
        # Store seconds as milliseconds-integer to keep the record integer-typed
        # without losing sub-second precision; convert back at money time.
        self.records.append(UsageRecord(
            conversation_id=self.conversation_id, business_id=self.business_id,
            turn_index=turn_index, provider="sarvam", model=model,
            unit_kind=UNIT_STT_AUDIO_SECONDS,
            unit_count=int((seconds * 1000).to_integral_value()),  # ms
            attempt=attempt, provider_processed=provider_processed,
            request_id=request_id))

    def record_llm(self, *, turn_index: int, usage: dict, model: str,
                   attempt: int, provider_processed: bool) -> None:
        # Prompt and completion are DISTINCT billing dimensions — never summed
        # into one "tokens" figure, because they are usually priced differently.
        prompt = int(usage.get("prompt_tokens", 0))
        completion = int(usage.get("completion_tokens", 0))
        self.records.append(UsageRecord(
            conversation_id=self.conversation_id, business_id=self.business_id,
            turn_index=turn_index, provider="gateway", model=model,
            unit_kind=UNIT_LLM_PROMPT_TOKENS, unit_count=prompt,
            attempt=attempt, provider_processed=provider_processed))
        self.records.append(UsageRecord(
            conversation_id=self.conversation_id, business_id=self.business_id,
            turn_index=turn_index, provider="gateway", model=model,
            unit_kind=UNIT_LLM_COMPLETION_TOKENS, unit_count=completion,
            attempt=attempt, provider_processed=provider_processed))

    def record_tts(self, *, turn_index: int, char_count: int, model: str,
                   attempt: int, provider_processed: bool) -> None:
        self.records.append(UsageRecord(
            conversation_id=self.conversation_id, business_id=self.business_id,
            turn_index=turn_index, provider="cartesia", model=model,
            unit_kind=UNIT_TTS_CHARACTERS, unit_count=int(char_count),
            attempt=attempt, provider_processed=provider_processed))

    # --- Idempotent ingest (fixture replay / event stream) -----------------
    def ingest(self, record: UsageRecord) -> bool:
        """Add a pre-built UsageRecord, deduped by identity. Returns True if
        newly added, False if it was already present (a replay/duplicate).

        This is what makes fixture reprocessing safe: feeding the same captured
        usage block twice yields identical totals, never doubled ones. The
        record's own conversation_id/business_id must match this meter's."""
        if (record.conversation_id != self.conversation_id
                or record.business_id != self.business_id):
            raise ValueError(
                "record correlation ids do not match this meter's call")
        if any(r.identity == record.identity for r in self.records):
            return False
        self.records.append(record)
        return True

    def ingest_all(self, records: list[UsageRecord]) -> dict[str, int]:
        """Ingest many records idempotently. Returns {'added': n, 'skipped': m}
        so a replay is visible in the numbers, not hidden."""
        added = skipped = 0
        for rec in records:
            if self.ingest(rec):
                added += 1
            else:
                skipped += 1
        return {"added": added, "skipped": skipped}

    # --- Aggregation -------------------------------------------------------
    def billable_records(self) -> list[UsageRecord]:
        """Only attempts the provider actually processed. A retry that the
        provider processed IS billable and stays in; a call that never reached
        the provider is excluded."""
        return [r for r in self.records if r.provider_processed]

    def retry_records(self) -> list[UsageRecord]:
        return [r for r in self.records if r.attempt > 1]

    def call_totals(self) -> dict[str, int]:
        """Per-unit-kind billable totals for the whole call (integer units)."""
        totals: dict[str, int] = {}
        for r in self.billable_records():
            totals[r.unit_kind] = totals.get(r.unit_kind, 0) + r.unit_count
        return totals

    def per_turn_totals(self) -> dict[int, dict[str, int]]:
        out: dict[int, dict[str, int]] = {}
        for r in self.billable_records():
            out.setdefault(r.turn_index, {})
            out[r.turn_index][r.unit_kind] = (
                out[r.turn_index].get(r.unit_kind, 0) + r.unit_count)
        return out

    def reconcile(self) -> None:
        """Assert per-turn billable units summed per unit-kind == call totals.
        Raises AssertionError if the books don't add up."""
        call = self.call_totals()
        summed: dict[str, int] = {}
        for _turn, kinds in self.per_turn_totals().items():
            for k, v in kinds.items():
                summed[k] = summed.get(k, 0) + v
        assert summed == call, (
            f"reconciliation failed: per-turn sum {summed} != call totals {call}")

    # --- Money (ONLY from a dated PricingBook) -----------------------------
    def estimate_micros(self, book: PricingBook) -> dict[str, Optional[int]]:
        """ESTIMATED cost in micro-currency (1e-6 units), per unit-kind, using
        ONLY dated/sourced prices. A unit-kind with no priced entry returns
        None (not zero) so 'unpriced' can never read as 'free'. This is
        ESTIMATED, never invoiced — no API here returns an invoiced amount, so
        there is nothing to reconcile it against yet.

        STT seconds are stored as ms; convert ms→seconds at money time only."""
        out: dict[str, Optional[int]] = {}
        for unit_kind, total in self.call_totals().items():
            entry = book.get(unit_kind)
            if entry is None:
                out[unit_kind] = None
                continue
            if unit_kind == UNIT_STT_AUDIO_SECONDS:
                seconds = Decimal(total) / Decimal(1000)  # ms → s
                money = seconds * entry.per_unit
            else:
                money = Decimal(total) * entry.per_unit
            micros = int((money * Decimal(1_000_000)).to_integral_value())
            out[unit_kind] = micros
        return out
