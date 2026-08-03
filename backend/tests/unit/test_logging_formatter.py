"""Tests for the JSON log formatter including extra fields."""

import json
import logging

from fonely.core.logging_config import JsonFormatter


def test_json_formatter_includes_extra_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="test message",
        args=(),
        exc_info=None,
    )
    record.business_id = 42  # type: ignore[attr-defined]
    record.latency_ms = 150  # type: ignore[attr-defined]
    record.intent = "book_appointment"  # type: ignore[attr-defined]

    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["message"] == "test message"
    assert parsed["level"] == "INFO"
    assert parsed["business_id"] == 42
    assert parsed["latency_ms"] == 150
    assert parsed["intent"] == "book_appointment"


def test_json_formatter_excludes_standard_attrs():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.WARNING,
        pathname="test.py",
        lineno=10,
        msg="warning message",
        args=(),
        exc_info=None,
    )

    output = formatter.format(record)
    parsed = json.loads(output)

    assert "lineno" not in parsed
    assert "pathname" not in parsed
    assert "funcName" not in parsed
    assert "processName" not in parsed
