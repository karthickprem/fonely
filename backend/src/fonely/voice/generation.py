"""Generation clock and stale-output coordination.

Ported from R&D live_poc.py and delegation.py with production
hardening: typed tokens, bounded state, explicit cleanup.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class GenerationToken:
    session_id: str
    turn_id: int
    generation_id: int
    created_at_ns: int


class GenerationClock:
    """Monotonic turn and generation counter for one voice session."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._turn_id = 0
        self._generation_id = 0
        self._lock = threading.Lock()

    def current(self) -> GenerationToken:
        with self._lock:
            return GenerationToken(
                session_id=self._session_id,
                turn_id=self._turn_id,
                generation_id=self._generation_id,
                created_at_ns=time.monotonic_ns(),
            )

    def next_turn(self) -> GenerationToken:
        with self._lock:
            self._turn_id += 1
            self._generation_id += 1
            return GenerationToken(
                session_id=self._session_id,
                turn_id=self._turn_id,
                generation_id=self._generation_id,
                created_at_ns=time.monotonic_ns(),
            )

    def advance_generation(self) -> GenerationToken:
        with self._lock:
            self._generation_id += 1
            return GenerationToken(
                session_id=self._session_id,
                turn_id=self._turn_id,
                generation_id=self._generation_id,
                created_at_ns=time.monotonic_ns(),
            )

    def is_current(self, token: GenerationToken) -> bool:
        with self._lock:
            return (
                token.session_id == self._session_id
                and token.turn_id == self._turn_id
                and token.generation_id == self._generation_id
            )

    @property
    def turn_count(self) -> int:
        with self._lock:
            return self._turn_id
