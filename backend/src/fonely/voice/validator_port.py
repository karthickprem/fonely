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

from .config import CONSEQUENTIAL_CLASSES, SpeechClass


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
    """Always BLOCK consequential speech.  ALLOW only non-consequential."""

    def validate_speech(
        self,
        text: str,
        speech_class: SpeechClass,
        *,
        session_id: str = "",
        turn_id: str = "",
        generation_id: int = 0,
    ) -> SpeechValidationResult:
        if speech_class in CONSEQUENTIAL_CLASSES:
            return SpeechValidationResult(
                decision=ValidationDecision.BLOCK,
                speech_class=speech_class,
                reason="consequential speech blocked: no accepted validator injected",
            )
        return SpeechValidationResult(
            decision=ValidationDecision.ALLOW,
            speech_class=speech_class,
            reason="non-consequential speech allowed",
        )
