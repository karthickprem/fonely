"""Operator diagnostic endpoints and session registry.

Provides coarse session state, provider health, queue depths,
and aggregate metrics without exposing transcripts or PII.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import SessionState
from .providers import credentials_ready, probe_credentials


@dataclass
class SessionInfo:
    session_id: str
    state: SessionState
    business_id: int
    started_at_ns: int
    turn_count: int = 0


class SessionRegistry:
    """Process-local bounded session registry for diagnostics."""

    def __init__(self, max_sessions: int = 100) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._lock = threading.Lock()
        self._max = max_sessions
        self._total_created = 0
        self._total_closed = 0
        self._total_failed = 0

    def register(self, info: SessionInfo) -> bool:
        with self._lock:
            if len(self._sessions) >= self._max:
                return False
            self._sessions[info.session_id] = info
            self._total_created += 1
            return True

    def update_state(self, session_id: str, state: SessionState, turn_count: int = 0) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].state = state
                self._sessions[session_id].turn_count = turn_count

    def unregister(self, session_id: str, failed: bool = False) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            if failed:
                self._total_failed += 1
            else:
                self._total_closed += 1

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            sessions = [
                {
                    "session_id": s.session_id,
                    "state": s.state,
                    "business_id": s.business_id,
                    "turn_count": s.turn_count,
                    "age_seconds": (time.monotonic_ns() - s.started_at_ns) / 1e9,
                }
                for s in self._sessions.values()
            ]
        return {
            "active_sessions": len(sessions),
            "total_created": self._total_created,
            "total_closed": self._total_closed,
            "total_failed": self._total_failed,
            "sessions": sessions,
            "credentials": probe_credentials(),
            "credentials_ready": credentials_ready(),
        }
