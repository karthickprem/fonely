"""LLM output sanitization for safe conversation responses."""

import re

_MARKDOWN_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MARKDOWN_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MARKDOWN_BULLET = re.compile(r"^[-*]\s+", re.MULTILINE)
_MARKDOWN_CODE = re.compile(r"```[\s\S]*?```")
_MARKDOWN_INLINE_CODE = re.compile(r"`([^`]+)`")
_MULTI_NEWLINE = re.compile(r"\n{2,}")
_MULTI_SPACE = re.compile(r" {2,}")

_MAX_LENGTH = 500
_FALLBACK = "Could you say that again?"


def sanitize_llm_response(text: str | None) -> str:
    if text is None or not text.strip():
        return _FALLBACK

    result = _MARKDOWN_CODE.sub("", text)
    result = _MARKDOWN_BOLD.sub(r"\1", result)
    result = _MARKDOWN_INLINE_CODE.sub(r"\1", result)
    result = _MARKDOWN_HEADER.sub("", result)
    result = _MARKDOWN_BULLET.sub("", result)
    result = _MULTI_NEWLINE.sub(" ", result)
    result = result.replace("\n", " ")
    result = _MULTI_SPACE.sub(" ", result)
    result = result.strip()

    if not result:
        return _FALLBACK

    if len(result) > _MAX_LENGTH:
        result = result[:_MAX_LENGTH]

    return result
