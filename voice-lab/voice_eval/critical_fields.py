"""Deterministic critical-field evidence and clarification policy."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum


class FieldStatus(StrEnum):
    COLLECTING = "collecting"
    AMBIGUOUS = "ambiguous"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    REQUIRE_DTMF = "require_dtmf"


@dataclass(frozen=True)
class ClarificationAct:
    act: str
    reason: str
    attempt: int
    expected_digits: int = 10
    preserve_prefix: str | None = None


@dataclass(frozen=True)
class PhoneNumberState:
    required_digits: int = 10
    candidate: str | None = None
    alternatives: tuple[str, ...] = ()
    attempt_count: int = 0
    ambiguity_positions: tuple[int, ...] = ()
    status: FieldStatus = FieldStatus.COLLECTING
    confirmed_value: str | None = None

    @property
    def authoritative_value(self) -> str | None:
        return self.confirmed_value if self.status == FieldStatus.CONFIRMED else None


POSITIVE_CONFIRMATIONS = frozenset({"yes", "correct", "ஆம்", "ஆமா", "சரி", "சரிதான்"})
NEGATIVE_CONFIRMATIONS = frozenset({"no", "wrong", "இல்லை", "தப்பு", "வேண்டாம்"})


def digits_only(value: str) -> str:
    return "".join(re.findall(r"\d", value))


def collect_phone_attempt(
    state: PhoneNumberState,
    transcript: str,
    *,
    alternatives: tuple[str, ...] = (),
    ambiguity_positions: tuple[int, ...] = (),
) -> tuple[PhoneNumberState, ClarificationAct | None]:
    """Replace the prior full attempt; never concatenate across attempts."""
    attempt = state.attempt_count + 1
    candidate = digits_only(transcript)
    alt_digits = tuple(digits_only(value) for value in alternatives if digits_only(value))
    ambiguous = bool(ambiguity_positions) or any(value != candidate for value in alt_digits)
    if len(candidate) == state.required_digits and not ambiguous:
        return (
            replace(
                state,
                candidate=candidate,
                alternatives=alt_digits,
                attempt_count=attempt,
                ambiguity_positions=(),
                status=FieldStatus.AWAITING_CONFIRMATION,
                confirmed_value=None,
            ),
            None,
        )
    if attempt >= 2:
        return (
            replace(
                state,
                candidate=candidate or None,
                alternatives=alt_digits,
                attempt_count=attempt,
                ambiguity_positions=ambiguity_positions,
                status=FieldStatus.REQUIRE_DTMF,
                confirmed_value=None,
            ),
            ClarificationAct("request_dtmf_phone", "voice_attempts_exhausted", attempt),
        )
    reason = "ambiguous_digits" if ambiguous else f"received_{len(candidate)}_digits"
    return (
        replace(
            state,
            candidate=candidate or None,
            alternatives=alt_digits,
            attempt_count=attempt,
            ambiguity_positions=ambiguity_positions,
            status=FieldStatus.AMBIGUOUS,
            confirmed_value=None,
        ),
        ClarificationAct("clarify_phone_number", reason, attempt),
    )


def collect_dtmf(state: PhoneNumberState, keypad_digits: str) -> tuple[PhoneNumberState, ClarificationAct | None]:
    candidate = digits_only(keypad_digits)
    if len(candidate) != state.required_digits:
        return (
            replace(state, candidate=None, status=FieldStatus.REQUIRE_DTMF, confirmed_value=None),
            ClarificationAct("request_dtmf_phone", f"received_{len(candidate)}_digits", state.attempt_count),
        )
    return replace(state, candidate=candidate, status=FieldStatus.AWAITING_CONFIRMATION), None


def apply_confirmation(state: PhoneNumberState, utterance: str) -> PhoneNumberState:
    tokens = {token for token in re.split(r"\s+", utterance.casefold().strip()) if token}
    positive = bool(tokens & POSITIVE_CONFIRMATIONS)
    negative = bool(tokens & NEGATIVE_CONFIRMATIONS)
    if state.status != FieldStatus.AWAITING_CONFIRMATION or not state.candidate:
        return state
    if positive and not negative:
        return replace(state, status=FieldStatus.CONFIRMED, confirmed_value=state.candidate)
    if negative and not positive:
        return replace(state, candidate=None, status=FieldStatus.COLLECTING, confirmed_value=None)
    return replace(state, status=FieldStatus.AMBIGUOUS, confirmed_value=None)


def grouped_phone_readback(candidate: str) -> tuple[str, str]:
    digits = digits_only(candidate)
    if len(digits) != 10:
        raise ValueError("phone readback requires exactly 10 digits")
    return digits[:5], digits[5:]
