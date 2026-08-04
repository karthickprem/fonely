# Critical-Field Policy

STT is evidence, not authoritative truth.

## Authority boundary

These values cannot become authoritative from an STT or LLM output:

- phone number;
- patient name;
- date/time;
- doctor/service;
- fee;
- confirmation/correction/negation;
- cancellation/rescheduling;
- booking or medical-success claims.

Required flow:

```text
raw speech hypothesis
→ preserved alternatives and raw confidence
→ deterministic format/domain validation
→ field-specific clarification
→ canonical readback
→ explicit confirmation
→ authoritative command
→ PostgreSQL commit
→ success speech
```

## Phone-number state

Implemented experimental state in `voice-lab/voice_eval/critical_fields.py`:

- exactly 10 digits;
- each full retry replaces the prior full candidate;
- attempts are never concatenated;
- ambiguous alternatives require clarification;
- after two failed voice attempts, transition to `require_dtmf`;
- keypad input must contain exactly 10 digits;
- grouped 5+5 readback;
- explicit confirmation before `authoritative_value` becomes non-null;
- mixed positive/negative confirmation remains ambiguous.

The browser lab does not prove telephony DTMF transport. Only the deterministic state contract is implemented.

## Other fields

Names, dates, times, doctors, services, fees, and negation still require typed state engines before production use. Current shadow correction may propose non-critical repair or `would_clarify`; it does not authorize a value.

## Fail-closed rules

- no guessed uncertain digits;
- no LLM confirmation authority;
- no unsupported entity invention;
- no unconfirmed value in application commands;
- no success speech before the outer transaction commits;
- raw wrong-script evidence remains visible after normalization.
