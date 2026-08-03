"""Structured logging configuration for Fonely.

JSON format for production/staging, human-readable for development.
Configure via FONELY_LOG_FORMAT=json|text and FONELY_LOG_LEVEL=INFO|DEBUG|etc.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

_STANDARD_LOG_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "exc_info",
        "exc_text",
        "message",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_ATTRS and key not in entry and not key.startswith("_"):
                try:
                    json.dumps(value)
                    entry[key] = value
                except (TypeError, ValueError):
                    pass
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = f"{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
        return json.dumps(entry, default=str)


def configure_logging(log_format: str = "text", log_level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(log_level.upper())

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s")
        )
    root.addHandler(handler)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
