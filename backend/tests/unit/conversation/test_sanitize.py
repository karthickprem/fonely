"""Unit tests for LLM output sanitization."""

from fonely.domain.conversation.sanitize import sanitize_llm_response


def test_strip_bold_markdown() -> None:
    assert sanitize_llm_response("**Hello** how are you?") == "Hello how are you?"


def test_strip_header_and_bullets() -> None:
    result = sanitize_llm_response("## Header\n- item1\n- item2")
    assert "##" not in result
    assert "-" not in result
    assert "Header" in result
    assert "item1" in result


def test_empty_returns_fallback() -> None:
    assert sanitize_llm_response("") == "Could you say that again?"
    assert sanitize_llm_response("   ") == "Could you say that again?"


def test_none_returns_fallback() -> None:
    assert sanitize_llm_response(None) == "Could you say that again?"


def test_truncate_long_text() -> None:
    result = sanitize_llm_response("x" * 1000)
    assert len(result) == 500


def test_strip_code_blocks() -> None:
    result = sanitize_llm_response("Here is code:\n```python\nprint('hi')\n```\nDone")
    assert "```" not in result
    assert "Done" in result


def test_collapse_multiple_newlines() -> None:
    result = sanitize_llm_response("Line one\n\n\nLine two")
    assert "\n" not in result
    assert "Line one" in result and "Line two" in result
