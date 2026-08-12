"""Deterministic mock STT, LLM, and TTS for provider-free pipeline testing.

Each mock produces realistic typed outputs from scripted conversation
flows without network calls or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockSTTResult:
    text: str
    language: str = "ta-Latn"
    is_final: bool = True
    confidence: float = 0.95


@dataclass
class MockLLMResult:
    text: str
    input_tokens: int = 50
    output_tokens: int = 30
    model: str = "mock-claude"


@dataclass
class MockTTSResult:
    text: str
    audio_bytes: int = 0
    characters: int = 0
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        self.characters = len(self.text)
        self.audio_bytes = self.characters * 40
        self.duration_ms = self.characters * 15.0


@dataclass
class ConversationTurn:
    caller_text: str
    expected_response: str
    asked_field: str | None = None
    speech_class: str = "non_consequential"
    caller_language: str = "ta-Latn"


@dataclass
class ScriptedConversation:
    scenario_id: str
    turns: list[ConversationTurn] = field(default_factory=list)
    expected_terminal: str = ""
    expected_max_turns: int = 12
    greeting: str = ""


class MockSTT:
    """Returns scripted transcriptions in order."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self._index = 0
        self._total_seconds = 0.0

    def transcribe(self) -> MockSTTResult | None:
        if self._index >= len(self._texts):
            return None
        text = self._texts[self._index]
        self._index += 1
        self._total_seconds += len(text.split()) * 0.4
        return MockSTTResult(text=text)

    @property
    def total_seconds(self) -> float:
        return self._total_seconds


class MockLLM:
    """Returns scripted responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._index = 0
        self._total_input = 0
        self._total_output = 0

    def generate(self, context: str = "") -> MockLLMResult:
        if self._index >= len(self._responses):
            return MockLLMResult(text="")
        text = self._responses[self._index]
        self._index += 1
        result = MockLLMResult(
            text=text, input_tokens=len(context.split()), output_tokens=len(text.split())
        )
        self._total_input += result.input_tokens
        self._total_output += result.output_tokens
        return result

    @property
    def total_tokens(self) -> tuple[int, int]:
        return self._total_input, self._total_output


class MockTTS:
    """Simulates TTS synthesis from text."""

    def __init__(self) -> None:
        self._total_characters = 0
        self._total_audio_bytes = 0

    def synthesize(self, text: str) -> MockTTSResult:
        result = MockTTSResult(text=text)
        self._total_characters += result.characters
        self._total_audio_bytes += result.audio_bytes
        return result

    @property
    def total_characters(self) -> int:
        return self._total_characters


def run_scripted_conversation(
    script: ScriptedConversation,
) -> dict[str, Any]:
    """Execute a full multi-turn mock STT→LLM→TTS conversation.

    Returns sanitized evidence without transcript text.
    """
    from .config import SpeechClass
    from .dialogue import DialogueState, count_questions, detect_filler
    from .generation import GenerationClock
    from .pipeline import PreTTSValidatorGate
    from .telemetry import VoiceTelemetryExporter
    from .validator_port import FailClosedValidatorStub

    stt = MockSTT([t.caller_text for t in script.turns])
    llm = MockLLM([t.expected_response for t in script.turns])
    tts = MockTTS()
    clock = GenerationClock(script.scenario_id)
    tel = VoiceTelemetryExporter(script.scenario_id)
    gate = PreTTSValidatorGate(FailClosedValidatorStub(), tel, clock)
    ds = DialogueState(max_turns=script.expected_max_turns)

    turn_evidence = []
    blocked_count = 0
    filler_count = 0
    multi_question_count = 0

    for i, turn in enumerate(script.turns):
        stt_result = stt.transcribe()
        if stt_result is None:
            break

        clock.next_turn()
        llm_result = llm.generate(stt_result.text)
        response = llm_result.text

        has_filler = detect_filler(response)
        if has_filler:
            filler_count += 1

        q_count = count_questions(response)
        if q_count > 1:
            multi_question_count += 1

        speech_class = (
            SpeechClass(turn.speech_class)
            if turn.speech_class != "non_consequential"
            else SpeechClass.NON_CONSEQUENTIAL
        )
        allowed = gate.check(response, speech_class)
        if not allowed:
            blocked_count += 1

        if allowed:
            tts_result = tts.synthesize(response)
            tel.record_tts_usage(tts_result.characters)

        tel.record_stt_usage(len(stt_result.text.split()) * 0.4)
        tel.record_llm_usage(llm_result.input_tokens, llm_result.output_tokens)

        ds.record_turn(response, asked_field=turn.asked_field)

        turn_evidence.append(
            {
                "turn": i + 1,
                "caller_length": len(turn.caller_text),
                "response_length": len(response),
                "speech_class": turn.speech_class,
                "allowed": allowed,
                "filler": has_filler,
                "questions": q_count,
                "asked_field": turn.asked_field,
            }
        )

        if ds.is_over_budget():
            ds.set_terminal("max_turns")
            break

    terminal = ds.terminal_reason or ("completed" if not ds.terminal else ds.terminal_reason)
    usage = tel.usage_summary()

    return {
        "scenario_id": script.scenario_id,
        "total_turns": ds.turn_count,
        "max_turns": script.expected_max_turns,
        "over_budget": ds.is_over_budget(),
        "repeated_questions": ds.has_repeated_question(),
        "terminal": terminal,
        "expected_terminal": script.expected_terminal,
        "blocked_count": blocked_count,
        "filler_count": filler_count,
        "multi_question_count": multi_question_count,
        "stt_seconds": usage["stt_seconds"],
        "llm_input_tokens": usage["llm_input_tokens"],
        "llm_output_tokens": usage["llm_output_tokens"],
        "tts_characters": usage["tts_characters"],
        "turns": turn_evidence,
        "pass": (
            ds.turn_count <= script.expected_max_turns
            and filler_count == 0
            and multi_question_count == 0
            and not ds.has_repeated_question()
        ),
    }
