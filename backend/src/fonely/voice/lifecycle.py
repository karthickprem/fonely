"""Voice session supervisor with explicit state machine.

Owns one voice session's complete lifecycle: state transitions,
provider clients, pipeline, generation coordination, telemetry,
and resource cleanup.  Every exit path converges on close().
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import SessionState, VoiceSessionConfig, _VALID_TRANSITIONS
from .generation import GenerationClock
from .telemetry import VoiceTelemetryExporter
from .validator_port import FailClosedValidatorStub, ValidatorPort

logger = logging.getLogger("fonely.voice.lifecycle")


class VoiceSessionSupervisor:
    """Manages one voice session from creation through cleanup."""

    def __init__(
        self,
        config: VoiceSessionConfig,
        *,
        validator: ValidatorPort | None = None,
    ) -> None:
        self._config = config
        self._state = SessionState.CREATED
        self._validator = validator or FailClosedValidatorStub()
        self._clock = GenerationClock(config.session_id)
        self._telemetry = VoiceTelemetryExporter(config.session_id)
        self._close_reason: str | None = None
        self._started_at_ns = time.monotonic_ns()
        self._closed = False
        self._duration_timer: asyncio.Task[None] | None = None
        self._reconnect_timer: asyncio.Task[None] | None = None

        self._telemetry.emit(
            "session_created",
            business_id=config.business_id,
            session_id=config.session_id,
        )

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def session_id(self) -> str:
        return self._config.session_id

    @property
    def config(self) -> VoiceSessionConfig:
        return self._config

    @property
    def clock(self) -> GenerationClock:
        return self._clock

    @property
    def telemetry(self) -> VoiceTelemetryExporter:
        return self._telemetry

    @property
    def validator(self) -> ValidatorPort:
        return self._validator

    def transition(self, target: SessionState) -> bool:
        allowed = _VALID_TRANSITIONS.get(self._state, frozenset())
        if target not in allowed:
            logger.warning(
                "invalid_transition",
                extra={
                    "session": self._config.session_id,
                    "from": self._state,
                    "to": target,
                },
            )
            return False
        previous = self._state
        self._state = target
        self._telemetry.emit(
            "state_transition",
            from_state=previous,
            to_state=target,
        )

        if target == SessionState.ACTIVE and self._duration_timer is None:
            self._duration_timer = asyncio.ensure_future(
                self._enforce_max_duration()
            )

        if target == SessionState.RECONNECTING:
            self._reconnect_timer = asyncio.ensure_future(
                self._enforce_reconnect_grace()
            )
        elif (
            target == SessionState.ACTIVE
            and self._reconnect_timer is not None
        ):
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

        return True

    async def _enforce_max_duration(self) -> None:
        try:
            await asyncio.sleep(self._config.limits.max_duration_seconds)
            if self._state in {SessionState.ACTIVE, SessionState.RECONNECTING}:
                await self.close("max_duration_exceeded")
        except asyncio.CancelledError:
            pass

    async def _enforce_reconnect_grace(self) -> None:
        try:
            await asyncio.sleep(self._config.limits.reconnect_grace_seconds)
            if self._state == SessionState.RECONNECTING:
                await self.close("reconnect_grace_expired")
        except asyncio.CancelledError:
            pass

    async def close(self, reason: str = "normal") -> dict[str, Any]:
        if self._closed:
            return self._telemetry.usage_summary()

        self._closed = True
        self._close_reason = reason

        if self._duration_timer is not None:
            self._duration_timer.cancel()
        if self._reconnect_timer is not None:
            self._reconnect_timer.cancel()

        if reason in {"normal", "max_duration_exceeded", "drain"}:
            if SessionState.DRAINING in _VALID_TRANSITIONS.get(self._state, frozenset()):
                self._state = SessionState.DRAINING
            self._state = SessionState.CLOSED
        else:
            self._state = SessionState.FAILED

        elapsed_ms = (time.monotonic_ns() - self._started_at_ns) / 1_000_000

        self._telemetry.emit(
            "session_closed",
            reason=reason,
            final_state=self._state,
            duration_ms=elapsed_ms,
            turn_count=self._clock.turn_count,
        )

        summary = self._telemetry.close()
        summary["close_reason"] = reason
        summary["final_state"] = self._state
        summary["duration_ms"] = elapsed_ms

        logger.info(
            "session_closed",
            extra={
                "session": self._config.session_id,
                "reason": reason,
                "state": self._state,
                "duration_ms": elapsed_ms,
            },
        )

        return summary
