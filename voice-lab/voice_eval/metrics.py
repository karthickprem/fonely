from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class ErrorCounts:
    substitutions: int
    insertions: int
    deletions: int
    reference_words: int

    @property
    def wer(self) -> float:
        if self.reference_words == 0:
            return 0.0 if self.insertions == 0 else 1.0
        return (self.substitutions + self.insertions + self.deletions) / self.reference_words


def normalize_transcript(text: str, locale: str) -> list[str]:
    text = unicodedata.normalize("NFC", text).casefold()
    text = re.sub(r"[^0-9a-z஀-௿₹]+", " ", text)
    return text.split()


def word_error_counts(reference: str, hypothesis: str, locale: str) -> ErrorCounts:
    ref = normalize_transcript(reference, locale)
    hyp = normalize_transcript(hypothesis, locale)
    rows = len(ref) + 1
    cols = len(hyp) + 1
    table: list[list[tuple[int, int, int, int]]] = [[(0, 0, 0, 0)] * cols for _ in range(rows)]
    for i in range(1, rows):
        table[i][0] = (i, 0, 0, i)
    for j in range(1, cols):
        table[0][j] = (j, 0, j, 0)
    for i in range(1, rows):
        for j in range(1, cols):
            if ref[i - 1] == hyp[j - 1]:
                table[i][j] = table[i - 1][j - 1]
                continue
            candidates = [
                (table[i - 1][j - 1][0] + 1, table[i - 1][j - 1][1] + 1, table[i - 1][j - 1][2], table[i - 1][j - 1][3]),
                (table[i][j - 1][0] + 1, table[i][j - 1][1], table[i][j - 1][2] + 1, table[i][j - 1][3]),
                (table[i - 1][j][0] + 1, table[i - 1][j][1], table[i - 1][j][2], table[i - 1][j][3] + 1),
            ]
            table[i][j] = min(candidates)
    _, substitutions, insertions, deletions = table[-1][-1]
    return ErrorCounts(substitutions, insertions, deletions, len(ref))


def score_critical_entities(entities: list[dict], hypothesis: str, locale: str) -> tuple[int, int]:
    hypothesis_tokens = normalize_transcript(hypothesis, locale)

    def contains_sequence(candidate: str) -> bool:
        candidate_tokens = normalize_transcript(candidate, locale)
        if not candidate_tokens:
            return False
        width = len(candidate_tokens)
        return any(
            hypothesis_tokens[index : index + width] == candidate_tokens
            for index in range(len(hypothesis_tokens) - width + 1)
        )

    critical_entities = [entity for entity in entities if entity.get("critical", True)]
    correct = 0
    for entity in critical_entities:
        acceptable = [entity["value"], *entity.get("variants", [])]
        if any(contains_sequence(value) for value in acceptable):
            correct += 1
    return correct, len(critical_entities)


def nearest_rank_percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def aggregate_results(results: list[dict], fixtures: dict[str, dict]) -> dict:
    passed = [result for result in results if result["status"] == "passed"]
    total_errors = sum(result["metrics"][name] for result in passed for name in ["substitutions", "insertions", "deletions"])
    total_reference = sum(result["metrics"]["reference_words"] for result in passed)
    modes: dict[str, dict] = {}
    for mode in sorted({result["provider"]["mode"] for result in results}):
        rows = [result for result in passed if result["provider"]["mode"] == mode]
        modes[mode] = {
            "count": len(rows),
            "macro_wer": mean([row["metrics"]["wer"] for row in rows]) if rows else None,
            "p50_ms": nearest_rank_percentile([row["timing"]["wall_ms"] for row in rows], 50),
            "p95_ms": nearest_rank_percentile([row["timing"]["wall_ms"] for row in rows], 95),
        }
    entity_correct = sum(row["metrics"]["critical_entity_correct"] for row in passed)
    entity_total = sum(row["metrics"]["critical_entity_total"] for row in passed)
    return {
        "total": len(results),
        "succeeded": len(passed),
        "failed": len(results) - len(passed),
        "micro_wer": total_errors / total_reference if total_reference else None,
        "macro_wer": mean([row["metrics"]["wer"] for row in passed]) if passed else None,
        "critical_entity_exactness": entity_correct / entity_total if entity_total else None,
        "by_mode": modes,
    }
