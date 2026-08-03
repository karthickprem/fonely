from __future__ import annotations

from dataclasses import dataclass
import re


CRITICAL_KINDS = {"intent", "name", "doctor", "service", "date", "time", "price", "quantity", "negation"}
CONFUSIONS = {
    "documentary": ("doctor appointment", False),
    "root channel": ("root canal", True),
    "preeya": ("Priya", True),
    "aminji karai": ("Aminjikarai", False),
    "six dirty": ("six thirty", True),
    # Evidence-backed founder benchmark confusions. These remain shadow-only
    # and require clarification because they change critical values.
    "பிரிய உபாகனம்": ("Dr. Priya-வை பாக்கணும்", True),
    "எவ்ளோ ஓ கிளாக்": ("eleven o'clock", True),
    "டாக்குமெண்ட் ரீட்ல": ("documentary illa", True),
}


@dataclass(frozen=True)
class ShadowCorrection:
    candidates: list[dict]
    proposed_transcript: str
    decision: str
    reasons: list[str]
    changed_critical_field: bool


def propose_shadow_correction(raw_transcript: str, critical_entities: list[dict]) -> ShadowCorrection:
    lower = raw_transcript.casefold()
    candidates = []
    proposed = raw_transcript
    changed_critical = False
    reasons = []
    for confusion, (replacement, always_critical) in CONFUSIONS.items():
        if confusion not in lower:
            continue
        acceptable_critical_values = [
            value.casefold()
            for entity in critical_entities
            if entity["kind"] in CRITICAL_KINDS
            for value in [entity["value"], *entity.get("variants", [])]
        ]
        affected = any(
            confusion in value or replacement.casefold() in value
            for value in acceptable_critical_values
        )
        candidate = re.sub(re.escape(confusion), replacement, proposed, flags=re.IGNORECASE)
        candidates.append({"from": confusion, "to": replacement, "text": candidate, "confidence": 0.9, "reason": "reviewed dental confusion pair"})
        if affected or always_critical:
            changed_critical = True
            reasons.append("critical value requires caller clarification")
        else:
            proposed = candidate
            reasons.append("high-confidence reviewed domain confusion")
    if not candidates:
        return ShadowCorrection([], raw_transcript, "unchanged", [], False)
    if changed_critical:
        return ShadowCorrection(candidates, raw_transcript, "would_clarify", reasons, True)
    return ShadowCorrection(candidates, proposed, "would_correct", reasons, False)
