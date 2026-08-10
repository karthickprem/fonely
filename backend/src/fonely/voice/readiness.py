"""Worker readiness gate including voice runtime preload.

Extends the backend lifespan/readiness pattern: readiness is false
until all voice dependencies are verified and a self-check passes.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger("fonely.voice.readiness")


class ReadinessState(StrEnum):
    NEW = "new"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class ReadinessSnapshot:
    state: ReadinessState
    ready: bool
    preload_ms: float = 0.0
    failure_reason: str = ""


class VoiceReadinessGate:
    """Single-flight preload with idempotent readiness check."""

    def __init__(self) -> None:
        self._state = ReadinessState.NEW
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._snapshot = ReadinessSnapshot(state=ReadinessState.NEW, ready=False)

    def preload(self) -> ReadinessSnapshot:
        with self._lock:
            if self._state in {ReadinessState.READY, ReadinessState.FAILED}:
                return self._snapshot
            if self._state == ReadinessState.LOADING:
                pass
            else:
                self._state = ReadinessState.LOADING
                self._do_preload()
                return self._snapshot

        self._event.wait(timeout=30.0)
        return self._snapshot

    @staticmethod
    def _ensure_nltk_data() -> None:
        import os

        if os.environ.get("NLTK_DATA"):
            return
        for candidate in (
            os.path.join(os.path.expanduser("~"), "nltk_data"),
            "/scratch/karthick/nltk_data",
        ):
            if os.path.isdir(os.path.join(candidate, "tokenizers", "punkt_tab")):
                os.environ["NLTK_DATA"] = candidate
                logger.info("nltk_data_resolved", extra={"path": candidate})
                return

    def _do_preload(self) -> None:
        start = time.monotonic()
        try:
            self._ensure_nltk_data()
            import fonely.voice.config
            import fonely.voice.context
            import fonely.voice.generation
            import fonely.voice.lifecycle
            import fonely.voice.telemetry
            import fonely.voice.validator_port
            from fonely.voice.providers import credentials_ready, probe_credentials

            cred_probe = probe_credentials()
            if not credentials_ready():
                missing = [k for k, v in cred_probe.items() if v != "SET"]
                elapsed = (time.monotonic() - start) * 1000
                self._snapshot = ReadinessSnapshot(
                    state=ReadinessState.FAILED,
                    ready=False,
                    preload_ms=elapsed,
                    failure_reason=f"missing_credentials:{','.join(missing)}",
                )
                self._state = ReadinessState.FAILED
                logger.error("voice_readiness_credentials_missing", extra={"missing": missing})
                return

            elapsed = (time.monotonic() - start) * 1000
            self._snapshot = ReadinessSnapshot(
                state=ReadinessState.READY,
                ready=True,
                preload_ms=elapsed,
            )
            self._state = ReadinessState.READY
            logger.info("voice_readiness_ready", extra={"preload_ms": elapsed})
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            self._snapshot = ReadinessSnapshot(
                state=ReadinessState.FAILED,
                ready=False,
                preload_ms=elapsed,
                failure_reason=type(exc).__name__,
            )
            self._state = ReadinessState.FAILED
            logger.error("voice_readiness_failed", extra={"error": type(exc).__name__})
        finally:
            self._event.set()

    def readiness(self) -> ReadinessSnapshot:
        return self._snapshot

    @property
    def is_ready(self) -> bool:
        return self._state == ReadinessState.READY
