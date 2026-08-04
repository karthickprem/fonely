"""Generation-aware session coordination for the bounded realtime voice POC."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable


class TranscriptStage(StrEnum):
    SPECULATIVE = "speculative"
    FINAL = "final"
    AUTHORITATIVE = "authoritative"


class DelegateStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    STALE = "stale"
    FAILED = "failed"


@dataclass(frozen=True)
class TurnToken:
    session_id: str
    turn_id: int
    generation_id: int


@dataclass(frozen=True)
class TranscriptEvidence:
    token: TurnToken
    stage: TranscriptStage
    text: str
    provider: str
    finalized: bool
    received_offset_ms: float

    @property
    def authoritative(self) -> bool:
        return self.stage == TranscriptStage.AUTHORITATIVE


@dataclass(frozen=True)
class DelegateResult:
    request_id: str
    token: TurnToken
    status: DelegateStatus
    payload: dict[str, Any] | None
    started_offset_ms: float
    completed_offset_ms: float
    error_category: str | None = None

    def sanitized_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["token"] = asdict(self.token)
        record["status"] = self.status.value
        record["payload"] = None
        return record


class GenerationClock:
    """Session-local turn/generation validity; never business authority."""

    def __init__(self, session_id: str):
        self._session_id = session_id
        self._turn_id = 0
        self._generation_id = 0

    @property
    def current(self) -> TurnToken:
        return TurnToken(self._session_id, self._turn_id, self._generation_id)

    def next_turn(self) -> TurnToken:
        self._turn_id += 1
        return self.current

    def advance_generation(self) -> TurnToken:
        self._generation_id += 1
        return self.current

    def is_current(self, token: TurnToken) -> bool:
        return token == self.current


class RealtimePOCCoordinator:
    """Own bounded read-only delegate lifecycle for one voice session."""

    def __init__(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        build_id: str | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.generations = GenerationClock(session_id)
        self.run_id = run_id
        self.build_id = build_id
        self.final_transcript_event_id: str | None = None
        self.final_transcript_text: str | None = None
        self.response_generation: int | None = None
        self._event_sink = event_sink or (lambda _event: None)
        self._clock = clock
        self._started = clock()
        self._task: asyncio.Task | None = None
        self._request_id: str | None = None
        self._closed = False

    @property
    def pending_tasks(self) -> int:
        return int(self._task is not None and not self._task.done())

    def _offset_ms(self) -> float:
        return (self._clock() - self._started) * 1000

    @property
    def event_sink(self):
        return self._event_sink

    def emit(self, event: str, **fields: Any) -> None:
        self._event_sink(
            {
                "schema_version": 1,
                "event": event,
                "run_id": self.run_id,
                "build_id": self.build_id,
                "session_id": self.generations.current.session_id,
                "turn_id": self.generations.current.turn_id,
                "generation_id": self.generations.current.generation_id,
                "offset_ms": self._offset_ms(),
                **fields,
            }
        )

    def record_transcript(
        self,
        text: str,
        *,
        finalized: bool,
        provider: str = "sarvam",
    ) -> TranscriptEvidence:
        token = self.generations.next_turn() if finalized else self.generations.current
        stage = TranscriptStage.FINAL if finalized else TranscriptStage.SPECULATIVE
        evidence = TranscriptEvidence(token, stage, text, provider, finalized, self._offset_ms())
        event_id = uuid.uuid4().hex
        if finalized:
            self.final_transcript_event_id = event_id
            self.final_transcript_text = text
        self.emit(
            "transcript_stage",
            event_id=event_id,
            stage=stage.value,
            finalized=finalized,
            text_length=len(text),
        )
        return evidence

    def mark_authoritative(self, evidence: TranscriptEvidence) -> TranscriptEvidence:
        if not evidence.finalized:
            raise ValueError("speculative transcript cannot become authoritative")
        return TranscriptEvidence(
            evidence.token,
            TranscriptStage.AUTHORITATIVE,
            evidence.text,
            evidence.provider,
            True,
            self._offset_ms(),
        )

    async def start_delegate(
        self,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        *,
        timeout_ms: int,
        context_trigger_id: str | None = None,
    ) -> DelegateResult:
        if self._closed:
            raise RuntimeError("coordinator is closed")
        await self.cancel_delegate("superseded")
        token = self.generations.current
        request_id = uuid.uuid4().hex
        started = self._offset_ms()
        self._request_id = request_id
        self.emit(
            "delegate_started",
            request_id=request_id,
            timeout_ms=timeout_ms,
            context_trigger_id=context_trigger_id,
            final_transcript_event_id=self.final_transcript_event_id,
        )
        self._task = asyncio.create_task(operation())
        task = self._task
        try:
            payload = await asyncio.wait_for(asyncio.shield(task), timeout_ms / 1000)
            status = DelegateStatus.COMPLETED if self.generations.is_current(token) else DelegateStatus.STALE
            if status == DelegateStatus.STALE:
                self.emit(
                    "delegate_suppressed",
                    request_id=request_id,
                    submitted_generation_id=token.generation_id,
                    reason="generation_changed",
                )
                payload = None
            else:
                self.emit("delegate_finished", request_id=request_id, outcome=status.value)
            self.emit(
                "delegate_disposition",
                request_id=request_id,
                outcome=f"discarded_{status.value}",
                payload_released=False,
                downstream_targets=0,
            )
            return DelegateResult(request_id, token, status, None, started, self._offset_ms())
        except TimeoutError:
            task.cancel()
            self.emit("delegate_finished", request_id=request_id, outcome=DelegateStatus.TIMED_OUT.value)
            self.emit("delegate_disposition", request_id=request_id, outcome="discarded_timed_out", payload_released=False, downstream_targets=0)
            return DelegateResult(request_id, token, DelegateStatus.TIMED_OUT, None, started, self._offset_ms())
        except asyncio.CancelledError:
            self.emit("delegate_finished", request_id=request_id, outcome=DelegateStatus.CANCELLED.value)
            self.emit("delegate_disposition", request_id=request_id, outcome="discarded_cancelled", payload_released=False, downstream_targets=0)
            return DelegateResult(request_id, token, DelegateStatus.CANCELLED, None, started, self._offset_ms())
        except Exception as exc:
            self.emit("delegate_finished", request_id=request_id, outcome=DelegateStatus.FAILED.value)
            self.emit("delegate_disposition", request_id=request_id, outcome="discarded_failed", payload_released=False, downstream_targets=0)
            return DelegateResult(
                request_id,
                token,
                DelegateStatus.FAILED,
                None,
                started,
                self._offset_ms(),
                error_category=type(exc).__name__,
            )
        finally:
            if self._task is task:
                self._task = None
                self._request_id = None

    async def interrupt(
        self,
        reason: str = "user_speech",
        *,
        logical_interruption_id: str | None = None,
    ) -> TurnToken:
        prior = self.generations.current
        token = self.generations.advance_generation()
        self.emit(
            "generation_advanced",
            old_generation_id=prior.generation_id,
            logical_interruption_id=logical_interruption_id,
            advance_count=1,
            reason=reason,
        )
        await self.cancel_delegate(reason)
        return token

    async def cancel_delegate(self, reason: str) -> None:
        if self._task and not self._task.done():
            self.emit("delegate_cancel_requested", request_id=self._request_id, reason=reason)
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        await self.cancel_delegate("session_closed")
        self.emit("coordinator_closed", pending_tasks=self.pending_tasks)


async def delayed_lookup(
    *,
    delay_ms: int,
    result: dict[str, Any],
    cooperative_cancel: bool = True,
    failure: str | None = None,
) -> dict[str, Any]:
    """Deterministic, read-only POC delegate with no network or backend access."""
    if delay_ms < 0:
        raise ValueError("delay_ms must be non-negative")
    if cooperative_cancel:
        await asyncio.sleep(delay_ms / 1000)
    else:
        deadline = time.monotonic() + delay_ms / 1000
        while time.monotonic() < deadline:
            try:
                await asyncio.sleep(min(0.01, max(0, deadline - time.monotonic())))
            except asyncio.CancelledError:
                continue
    if failure:
        raise RuntimeError(failure)
    return dict(result)


def ensure_async_callable(operation: Callable[..., Any]) -> None:
    if not inspect.iscoroutinefunction(operation):
        raise TypeError("delegate operation must be asynchronous")
