"""Deterministic retrieval over Karthick's Chennai dental speech examples."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

TAMIL = re.compile(r"[஀-௿]")
WORDS = re.compile(r"[A-Za-z0-9]+|[஀-௿]+")

INTENT_TERMS = {
    "booking": ["appointment", "book", "slot", "அப்பாயின்ட்மெண்ட்"],
    "doctor_availability": ["doctor", "dentist", "dr.", "available", "free", "டாக்டர்"],
    "reschedule": ["reschedule", "change", "earlier", "later", "மாத்த", "வேற time"],
    "cancel": ["cancel", "வேணாம்", "வர முடியாது"],
    "pain": ["pain", "tooth", "pallu", "vali", "வலி", "பல்லு"],
    "urgent": ["urgent", "emergency", "bleeding", "swelling", "ரத்தம்", "வீக்கம்"],
    "fee": ["fee", "price", "rate", "cost", "evlo", "எவ்ளோ", "₹"],
    "location": ["where", "location", "address", "enga", "எங்க", "landmark"],
    "service": ["service", "treatment", "cleaning", "scaling", "root canal"],
    "handoff": ["human", "person", "receptionist", "staff"],
    "clarification": ["which", "when", "what", "எது", "எப்ப", "என்ன"],
}

EMOTION_TERMS = {
    "angry": ["angry", "worst", "useless", "complaint", "கோபம்"],
    "anxious": ["scared", "worried", "bayam", "பயம்", "pain", "வலி"],
    "hurried": ["quick", "fast", "urgent", "office", "வேகமா"],
    "confused": ["not sure", "don't know", "confused", "puriyala", "தெரியல"],
}


@dataclass(frozen=True)
class StyleQuery:
    text: str
    intents: frozenset[str]
    language_mix: str
    emotion: str
    safety_level: str
    stage: str


def normalize(text: str) -> set[str]:
    return {word.casefold() for word in WORDS.findall(text)}


def detect_language_mix(text: str) -> str:
    tamil = bool(TAMIL.search(text))
    latin = bool(re.search(r"[A-Za-z]", text))
    if tamil and latin:
        return "balanced_tanglish"
    if tamil:
        return "tamil_heavy"
    if latin:
        return "roman_tanglish"
    return "mixed"


def term_matches(text: str, term: str) -> bool:
    term = term.casefold()
    if re.fullmatch(r"[a-z0-9.]+", term):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))
    return term in text


def matched_labels(text: str, rules: dict[str, list[str]]) -> set[str]:
    lower = text.casefold()
    return {
        label
        for label, terms in rules.items()
        if any(term_matches(lower, term) for term in terms)
    }


def build_query(text: str) -> StyleQuery:
    intents = matched_labels(text, INTENT_TERMS) or {"clarification"}
    emotions = matched_labels(text, EMOTION_TERMS)
    emotion = next(iter(emotions), "calm")
    safety = "emergency" if "urgent" in intents else "medical" if "pain" in intents else "routine"
    stage = (
        "collect_details"
        if any(term in text.casefold() for term in ["name", "peru", "பேரு", "age", "phone"])
        else "availability"
        if any(term in intents for term in ["booking", "reschedule", "cancel"])
        else "clarification"
        if "?" in text or "clarification" in intents
        else "response"
    )
    return StyleQuery(
        text=text,
        intents=frozenset(intents),
        language_mix=detect_language_mix(text),
        emotion=emotion,
        safety_level=safety,
        stage=stage,
    )


class ChennaiStyleRetriever:
    def __init__(self, corpus_path: Path):
        payload = json.loads(corpus_path.read_text())
        self._examples = payload["examples"]

    def retrieve(self, text: str, limit: int = 3) -> list[dict]:
        query = build_query(text)
        query_words = normalize(text)
        scored: list[tuple[int, str, dict]] = []
        for example in self._examples:
            score = 0
            example_intents = set(example["intents"])
            intent_matches = query.intents.intersection(example_intents)
            if not intent_matches:
                continue
            score += min(10, len(intent_matches) * 5)
            score -= len(example_intents - query.intents) * 2
            if example["id"].startswith("curated-"):
                score += 4
            if query.language_mix == example["language_mix"]:
                score += 3
            elif {query.language_mix, example["language_mix"]} <= {
                "balanced_tanglish",
                "roman_tanglish",
                "tamil_heavy",
            }:
                score += 1
            if query.emotion == example["emotion"]:
                score += 2
            if query.safety_level == example["safety_level"]:
                score += 5
            if query.stage == example["stage"]:
                score += 4
            score += min(4, len(query_words.intersection(normalize(example["caller"]))))
            scored.append((score, example["id"], example))

        scored.sort(key=lambda item: (-item[0], item[1]))
        selected: list[dict] = []
        seen_scenarios: set[str] = set()
        minimum_score = max(1, scored[0][0] - 4) if scored else 1
        for score, _, example in scored:
            if score < minimum_score or example["scenario"] in seen_scenarios:
                continue
            selected.append(example)
            seen_scenarios.add(example["scenario"])
            if len(selected) == limit:
                break
        return selected

    @staticmethod
    def render(examples: list[dict], actual_user_text: str) -> str:
        if not examples:
            return actual_user_text
        references = "\n".join(
            f"Caller style: {example['caller']}\nTTS-ready natural reply pattern: {example['agent_tts']}"
            for example in examples
        )
        return (
            "<chennai_style_references>\n"
            "These are speech-style references only. Do not copy names, facts, slots, "
            "bookings, actions, or promises. Adapt the rhythm and warmth. The references "
            "include a Roman caller example and a TTS-ready Tamil-script reply pattern. "
            "Write Tamil words in Tamil script for TTS.\n"
            f"{references}\n"
            "</chennai_style_references>\n\n"
            f"Actual caller: {actual_user_text}"
        )
