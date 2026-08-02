"""Build a sanitized local style corpus from Karthick's conversation pack."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

SECTION_INTENTS = {
    "A": ["booking"],
    "B": ["existing_appointment"],
    "C": ["medical"],
    "D": ["chennai_tanglish"],
    "E": ["clarification"],
    "F": ["speech_repair"],
    "G": ["clinic_information"],
    "H": ["accessibility"],
    "I": ["availability"],
    "J": ["edge_case"],
}

INTENT_RULES = {
    "booking": ["appointment", "book", "slot", "reserve"],
    "doctor_availability": ["doctor", "dentist", "dr.", "available", "free-aa"],
    "pain": ["pain", "vali", "வலி", "pallu", "tooth", "sensitive"],
    "fee": ["fee", "price", "rate", "cost", "evlo", "எவ்ளோ", "discount"],
    "location": ["location", "address", "landmark", "parking", "enga", "எங்க"],
    "service": ["service", "treatment", "cleaning", "scaling", "root canal", "extraction"],
    "reschedule": ["reschedule", "change", "earlier", "later", "running late"],
    "cancel": ["cancel", "cancellation"],
    "emergency": ["emergency", "bleeding", "swelling", "unconscious", "trauma", "swallowed"],
    "handoff": ["human", "receptionist", "transfer", "callback"],
    "privacy": ["privacy", "records", "another patient", "verification"],
    "speech_repair": ["noise", "interrupt", "audio", "hear", "fast", "recognition"],
    "accessibility": ["child", "senior", "hearing", "quiet", "wheelchair", "pregnant"],
}

UNSAFE_OPERATIONAL = re.compile(
    r"\b(book(?:ed)?|confirm(?:ed|ation)?|reserve(?:d)?|cancel(?:led)?|reschedul(?:ed|e)|"
    r"message.*(?:send|varum|anuppu)|alert|transfer|retry|waitlist)\b|"
    r"book aayiduchu|confirm aayiduchu|panniyachu|pannitten|பண்ணிட்டேன்|confirm ஆச்சு",
    re.IGNORECASE,
)
PLACEHOLDER = re.compile(r"\{[^}]+\}|`[^`]+`")
TAMIL = re.compile(r"[஀-௿]")


def term_matches(text: str, term: str) -> bool:
    term = term.casefold()
    if re.fullmatch(r"[a-z0-9.]+", term):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))
    return term in text


def infer_intents(section: str, title: str, caller: str) -> list[str]:
    text = f"{title} {caller}".casefold()
    intents = {
        intent
        for intent, terms in INTENT_RULES.items()
        if any(term_matches(text, term) for term in terms)
    }
    if not intents:
        intents.update(SECTION_INTENTS.get(section, ["general"]))
    return sorted(intents)


def infer_language_mix(caller: str) -> str:
    tamil_chars = len(TAMIL.findall(caller))
    latin_words = len(re.findall(r"[A-Za-z]+", caller))
    if tamil_chars and latin_words:
        return "balanced_tanglish"
    if tamil_chars:
        return "tamil_heavy"
    if latin_words >= 2:
        return "roman_tanglish"
    return "mixed"


def infer_emotion(title: str, delivery: str) -> str:
    text = f"{title} {delivery}".lower()
    for emotion, terms in {
        "urgent": ["urgent", "emergency", "alert"],
        "anxious": ["anxious", "pain", "worried", "reassuring"],
        "angry": ["angry", "abusive", "firm"],
        "confused": ["confused", "ambigu", "unclear", "correction"],
        "hurried": ["fast", "hurried", "practical"],
    }.items():
        if any(term in text for term in terms):
            return emotion
    return "calm"


def infer_stage(agent: str) -> str:
    lower = agent.lower()
    if any(term in lower for term in ["name", "peru", "பேரு", "age", "phone"]):
        return "collect_details"
    if any(term in lower for term in ["available", "slot", "time", "date", "day"]):
        return "availability"
    if "?" in agent:
        return "clarification"
    return "response"


def infer_safety(section: str, title: str) -> str:
    text = title.lower()
    if section == "C" and any(
        term in text for term in ["swelling", "bleeding", "unconscious", "trauma", "swallowed"]
    ):
        return "emergency"
    if section == "C" or any(term in text for term in ["pain", "medication", "diagnos"]):
        return "medical"
    return "routine"


def safe_pair(caller: str, agent: str, action_between: bool) -> bool:
    if action_between:
        return False
    if PLACEHOLDER.search(caller) or PLACEHOLDER.search(agent):
        return False
    if UNSAFE_OPERATIONAL.search(agent):
        return False
    if len(agent.split()) > 32:
        return False
    return bool(caller.strip() and agent.strip())


CURATED_ANCHORS = [
    {
        "id": "curated-booking",
        "section": "A",
        "scenario": "Appointment request needs reason",
        "caller": "Enakku appointment venum nga.",
        "agent": "Seringa, edhukkaaga doctor-a paakanum?",
        "agent_tts": "சரிங்க, எதுக்காக doctor-ஐ பாக்கணும்?",
        "delivery": "Warm and concise.",
        "intents": ["booking"],
        "language_mix": "roman_tanglish",
        "emotion": "calm",
        "safety_level": "routine",
        "stage": "clarification",
    },
    {
        "id": "curated-doctor",
        "section": "A",
        "scenario": "Doctor availability without preference",
        "caller": "Doctor eppo available-aa iruppaanga?",
        "agent": "Endha doctor venumnu sollunga nga?",
        "agent_tts": "எந்த doctor வேணும்னு சொல்லுங்க ங்க?",
        "delivery": "Guiding and brief.",
        "intents": ["doctor_availability"],
        "language_mix": "roman_tanglish",
        "emotion": "calm",
        "safety_level": "routine",
        "stage": "clarification",
    },
    {
        "id": "curated-fee",
        "section": "G",
        "scenario": "Known scaling fee",
        "caller": "Scaling rate evlo nga?",
        "agent": "Scaling 800 rupees nga. Consultation slot paakkava?",
        "agent_tts": "Scaling 800 rupees ங்க. Consultation slot பாக்கவா?",
        "delivery": "Direct and friendly.",
        "intents": ["fee", "service"],
        "language_mix": "roman_tanglish",
        "emotion": "calm",
        "safety_level": "routine",
        "stage": "response",
    },
    {
        "id": "curated-location",
        "section": "G",
        "scenario": "Known clinic location",
        "caller": "Clinic enga irukku nga?",
        "agent": "Namma clinic Aminjikarai-la irukku nga.",
        "agent_tts": "நம்ம clinic Aminjikarai-ல இருக்கு ங்க.",
        "delivery": "Clear and locally natural.",
        "intents": ["location"],
        "language_mix": "roman_tanglish",
        "emotion": "calm",
        "safety_level": "routine",
        "stage": "response",
    },
]


def parse_pack(text: str) -> list[dict]:
    records: list[dict] = []
    section = ""
    title = ""
    delivery = ""
    last_caller: str | None = None
    action_between = False

    for raw in text.splitlines():
        line = raw.strip()
        section_match = re.match(r"## ([A-J])\.", line)
        if section_match:
            section = section_match.group(1)
            continue
        title_match = re.match(r"### (\d{2,3})\s+[—-]\s+(.+)", line)
        if title_match:
            title = title_match.group(2).strip()
            delivery = ""
            last_caller = None
            action_between = False
            continue
        if line.startswith("DELIVERY:"):
            delivery = line.removeprefix("DELIVERY:").strip()
            continue
        if line.startswith("CALLER:"):
            last_caller = line.removeprefix("CALLER:").strip()
            action_between = False
            continue
        if line.startswith("ACTION:") or line.startswith("ACTION RESULT:"):
            action_between = True
            continue
        if line.startswith("AGENT:") and last_caller is not None:
            agent = line.removeprefix("AGENT:").strip()
            if safe_pair(last_caller, agent, action_between):
                records.append(
                    {
                        "id": f"{section.lower()}-{len(records)+1:03d}",
                        "section": section,
                        "scenario": title,
                        "caller": last_caller,
                        "agent": agent,
                        "delivery": delivery,
                        "intents": infer_intents(section, title, last_caller),
                        "language_mix": infer_language_mix(last_caller),
                        "emotion": infer_emotion(title, delivery),
                        "safety_level": infer_safety(section, title),
                        "stage": infer_stage(agent),
                    }
                )
            last_caller = None
            action_between = False

    records.extend(CURATED_ANCHORS)
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    source_bytes = args.source.read_bytes()
    records = parse_pack(source_bytes.decode("utf-8"))
    payload = {
        "version": 1,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_title": "Fonely Chennai Tanglish Dental Conversation Pack v1.0",
        "policy": (
            "Style references only. Never copy facts, placeholders, actions, or operational claims."
        ),
        "examples": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {len(records)} sanitized examples to {args.output}")


if __name__ == "__main__":
    main()
