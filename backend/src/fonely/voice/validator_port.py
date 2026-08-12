"""Fail-closed typed validator interface and stub.

The rejected validator (#57, holdout FAIL 5/40) must NOT be embedded as
trusted production logic.  This port defines the typed boundary; a
FailClosedValidatorStub blocks all consequential speech until an
independently accepted validator is injected.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol

from .config import SpeechClass


class ValidationDecision(enum.StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class SpeechValidationResult:
    decision: ValidationDecision
    speech_class: SpeechClass
    reason: str
    source: str = "stub"


class ValidatorPort(Protocol):
    def validate_speech(
        self,
        text: str,
        speech_class: SpeechClass,
        *,
        session_id: str = "",
        turn_id: str = "",
        generation_id: int = 0,
    ) -> SpeechValidationResult: ...


class FailClosedValidatorStub:
    """Always BLOCK consequential or unclassified speech.

    ALLOW only explicitly NON_CONSEQUENTIAL.  Unknown or missing
    speech class defaults to BLOCK — the validator cannot trust
    caller-supplied classification.
    """

    def validate_speech(
        self,
        text: str,
        speech_class: SpeechClass,
        *,
        session_id: str = "",
        turn_id: str = "",
        generation_id: int = 0,
    ) -> SpeechValidationResult:
        if speech_class == SpeechClass.NON_CONSEQUENTIAL:
            return SpeechValidationResult(
                decision=ValidationDecision.ALLOW,
                speech_class=speech_class,
                reason="non-consequential speech allowed",
            )
        return SpeechValidationResult(
            decision=ValidationDecision.BLOCK,
            speech_class=speech_class,
            reason="consequential/unclassified speech blocked: no accepted validator",
        )
