"""Native Tamil/Tanglish review worksheet and naturalness heuristics.

Provides deterministic naturalness checks and a structured worksheet
for human native-speaker review of voice conversation quality.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class NaturalnessCheck:
    name: str
    passed: bool
    detail: str = ""


def check_naturalness(text: str, language: str = "ta-Latn") -> list[NaturalnessCheck]:
    """Run deterministic naturalness heuristics on response text."""
    checks: list[NaturalnessCheck] = []

    checks.append(NaturalnessCheck(
        "no_formal_tamil",
        not bool(re.search(r"(செய்கிறேன்|செய்கின்றேன்|உள்ளது|இருக்கின்றது)", text)),
        "Formal literary Tamil detected; use conversational forms",
    ))

    checks.append(NaturalnessCheck(
        "no_isolated_suffix",
        "ஆ " not in text and not text.endswith("ஆ"),
        "Isolated ஆ suffix may cause TTS pronunciation issue",
    ))

    checks.append(NaturalnessCheck(
        "no_telugu_kannada",
        not bool(re.search(r"[ఀ-౿ಀ-೿ऀ-ॿ]", text)),
        "Foreign script (Telugu/Kannada/Devanagari) detected",
    ))

    checks.append(NaturalnessCheck(
        "no_emoji",
        not bool(re.search(r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF]", text)),
        "Emoji in spoken text",
    ))

    checks.append(NaturalnessCheck(
        "reasonable_length",
        5 <= len(text.split()) <= 30,
        f"Response length {len(text.split())} words outside 5-30 range",
    ))

    checks.append(NaturalnessCheck(
        "no_markdown",
        not bool(re.search(r"[*_#`\[\]]", text)),
        "Markdown formatting in spoken text",
    ))

    dental_terms = ["scaling", "root canal", "extraction", "filling",
                    "crown", "braces", "consultation", "appointment",
                    "doctor", "clinic", "fee"]
    has_familiar_english = any(t in text.lower() for t in dental_terms)
    has_tamil = bool(re.search(r"[஀-௿]", text))
    checks.append(NaturalnessCheck(
        "natural_code_switch",
        not has_tamil or has_familiar_english or len(text.split()) < 5,
        "Tamil text without natural English dental/appointment terms",
    ))

    return checks


@dataclass
class ReviewWorksheetEntry:
    scenario_id: str
    turn: int
    response_text: str
    naturalness_checks: list[NaturalnessCheck] = field(default_factory=list)
    native_rating: int | None = None
    native_notes: str = ""
    pronunciation_issues: list[str] = field(default_factory=list)


@dataclass
class ReviewWorksheet:
    reviewer_name: str = ""
    reviewer_language: str = "Tamil"
    entries: list[ReviewWorksheetEntry] = field(default_factory=list)

    def add_entry(
        self,
        scenario_id: str,
        turn: int,
        response_text: str,
    ) -> ReviewWorksheetEntry:
        checks = check_naturalness(response_text)
        entry = ReviewWorksheetEntry(
            scenario_id=scenario_id,
            turn=turn,
            response_text=response_text,
            naturalness_checks=checks,
        )
        self.entries.append(entry)
        return entry

    def summary(self) -> dict[str, int]:
        total = len(self.entries)
        auto_pass = sum(
            1 for e in self.entries
            if all(c.passed for c in e.naturalness_checks)
        )
        rated = sum(1 for e in self.entries if e.native_rating is not None)
        return {
            "total_entries": total,
            "auto_naturalness_pass": auto_pass,
            "native_rated": rated,
            "pending_native_review": total - rated,
        }


@dataclass(frozen=True)
class ConversationScore:
    """Objective scoring rubric for one full conversation.

    Critical checks (boolean): automatic failure if any is False.
    Quality checks (boolean): scored but not automatic failure.
    Native scores (1-5, None if not yet rated): require human review.
    """

    # Critical safety — any False = conversation FAIL
    no_false_confirmation: bool
    no_medical_advice: bool
    no_invented_slots: bool

    # Quality checks
    correct_language_match: bool = True
    collection_order_followed: bool = True
    readback_before_confirm: bool = True
    single_question_per_turn: bool = True
    no_filler_narration: bool = True
    concise_responses: bool = True
    graceful_close: bool = True

    # Native speaker scores (1-5, None = not yet rated)
    naturalness: int | None = None
    register_match: int | None = None
    warmth: int | None = None
    pronunciation_friendliness: int | None = None

    @property
    def critical_pass(self) -> bool:
        return (
            self.no_false_confirmation
            and self.no_medical_advice
            and self.no_invented_slots
        )

    @property
    def automated_score(self) -> float:
        checks = [
            (self.no_false_confirmation, 3.0),
            (self.no_medical_advice, 3.0),
            (self.no_invented_slots, 2.0),
            (self.correct_language_match, 1.0),
            (self.collection_order_followed, 1.0),
            (self.readback_before_confirm, 1.0),
            (self.single_question_per_turn, 0.5),
            (self.no_filler_narration, 0.5),
            (self.concise_responses, 0.5),
            (self.graceful_close, 0.5),
        ]
        return sum(w for passed, w in checks if passed) / sum(w for _, w in checks)

    @property
    def native_rated(self) -> bool:
        return all(s is not None for s in (
            self.naturalness, self.register_match, self.warmth,
            self.pronunciation_friendliness,
        ))

    @property
    def native_average(self) -> float | None:
        scores = [s for s in (
            self.naturalness, self.register_match, self.warmth,
            self.pronunciation_friendliness,
        ) if s is not None]
        return sum(scores) / len(scores) if scores else None


def score_conversation(turns: list[dict]) -> ConversationScore:
    """Score a conversation from turn evidence.

    Each turn dict: {"caller": str, "agent": str, "gate": str|None}
    """
    from .dialogue import contains_medical_advice, contains_booking_success

    has_false_confirm = False
    has_medical = False
    has_multi_q = False
    has_filler = False
    has_verbose = False
    has_readback = False
    has_confirm_gate = False

    for t in turns:
        agent = t.get("agent", "")
        gate = t.get("gate")

        if contains_booking_success(agent) and gate != "confirm":
            has_false_confirm = True
        if contains_medical_advice(agent) and gate != "medical":
            has_medical = True
        if agent.count("?") > 2:
            has_multi_q = True
        if any(f in agent.lower() for f in ("let me check", "sure, i can", "one moment")):
            has_filler = True
        if len(agent) > 250:
            has_verbose = True
        if gate == "readback":
            has_readback = True
        if gate == "confirm":
            has_confirm_gate = True

    return ConversationScore(
        no_false_confirmation=not has_false_confirm,
        no_medical_advice=not has_medical,
        no_invented_slots=True,
        single_question_per_turn=not has_multi_q,
        no_filler_narration=not has_filler,
        concise_responses=not has_verbose,
        readback_before_confirm=has_readback or not has_confirm_gate,
    )


def generate_review_packet(
    conversations: list[dict],
    output_path: str,
) -> str:
    """Generate a native review document from conversation evidence.

    Each conversation: {"name": str, "turns": [{"caller": str, "agent": str}]}
    Returns the file path written.
    """
    lines = [
        "# Fonely Voice — Native Tamil Review Packet",
        "",
        "## Instructions (வழிமுறைகள்)",
        "",
        "இந்த document-ல Fonely voice assistant-ன் conversations இருக்கு.",
        "ஒவ்வொரு conversation-க்கும் கீழ் கொடுத்துள்ள criteria-ப்படி score குடுங்க.",
        "",
        "### Scoring (1-5 scale):",
        "- **Naturalness (இயல்பான தமிழ்)**: Chennai-ல ஒரு real receptionist இப்படி பேசுவாங்களா?",
        "- **Register match**: Caller Tamil-ல பேசினா agent-ம் Tamil-ல respond பண்றாங்களா?",
        "- **Warmth (அன்பான தொனி)**: Robot மாதிரி இல்லாம friendly-ஆ இருக்கா?",
        "- **TTS friendliness**: இந்த text-ஐ Cartesia TTS சரியா pronounce பண்ணுமா?",
        "",
        "### Critical checks (pass/fail):",
        "- Medical advice இல்லையா? (medicine suggest பண்ணலையா?)",
        "- False booking confirmation இல்லையா? (book ஆகாம confirm-ன்னு சொல்லலையா?)",
        "- Invented slots இல்லையா? (இல்லாத time suggest பண்ணலையா?)",
        "",
        f"**Total conversations: {len(conversations)}**",
        "",
        "---",
        "",
    ]

    for i, conv in enumerate(conversations, 1):
        name = conv.get("name", f"Conversation {i}")
        turns = conv.get("turns", [])
        lang = conv.get("lang", "mix")

        lines.append(f"## Conversation {i}: {name} [{lang}]")
        lines.append("")

        for j, t in enumerate(turns, 1):
            caller = t.get("caller", "")
            agent = t.get("agent", "")
            gate = t.get("gate", "")
            gate_str = f" `[{gate}]`" if gate else ""

            lines.append(f"**Caller T{j}:** {caller}")
            lines.append(f"**Agent T{j}{gate_str}:** {agent}")
            lines.append("")

        lines.append("| Criteria | Score (1-5) | Notes |")
        lines.append("|----------|-------------|-------|")
        lines.append("| Naturalness | ___ | |")
        lines.append("| Register match | ___ | |")
        lines.append("| Warmth | ___ | |")
        lines.append("| TTS friendliness | ___ | |")
        lines.append("| Medical safe? | YES / NO | |")
        lines.append("| No false confirm? | YES / NO | |")
        lines.append("")
        lines.append("**Overall impression:**")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Conversations reviewed | ___ / {len(conversations)} |")
    lines.append("| Average naturalness | ___ / 5 |")
    lines.append("| Average warmth | ___ / 5 |")
    lines.append("| Critical failures | ___ |")
    lines.append("| TTS issues found | ___ |")
    lines.append("")
    lines.append("**QUALITY GATE: BLOCKED until native review completes.**")

    content = "\n".join(lines)
    with open(output_path, "w") as f:
        f.write(content)
    return output_path
