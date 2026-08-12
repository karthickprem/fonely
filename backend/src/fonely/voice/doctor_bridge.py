"""Agent↔doctor bridge — the seam for asking a human when the agent lacks info.

When the agent needs something it cannot resolve from the database (an
unconfirmed day's availability, a special instruction, an approval), it posts
a question here and awaits a response. In the demo the transport is the owner
panel over a WebSocket; in production it is durable WhatsApp messaging.

LIMITATIONS of this in-memory implementation, explicit so nobody mistakes it
for the production path:
  - state is in-memory: a process restart loses every open query
  - a query unanswered within the timeout is dropped, not retried
  - delivery is not durable and not acknowledged

Durable owner messaging is Dev3's lane. This module exists so the pipeline can
be wired and demoed against the real behaviour (agent pauses, human answers,
agent resumes) without waiting on that. The DoctorBridge Protocol is the seam:
swap the in-memory impl for a WhatsApp-backed one without touching the pipeline.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

logger = logging.getLogger("fonely.voice.doctor_bridge")


@dataclass
class DoctorQuery:
    question: str
    from_patient: str
    timestamp: datetime
    response: str | None = None
    responded_at: datetime | None = None

    @property
    def answered(self) -> bool:
        return self.response is not None


class DoctorBridge(Protocol):
    """Seam between the pipeline and whatever reaches a human.

    The pipeline depends on this Protocol, not the in-memory class, so a
    WhatsApp-backed durable implementation drops in unchanged.
    """
    async def ask_doctor(self, question: str, patient_context: str = "") -> DoctorQuery: ...
    async def wait_for_response(self, query: DoctorQuery, timeout: float = 60.0) -> str | None: ...


class InMemoryDoctorBridge:
    """In-memory, single-process DoctorBridge for demo and tests.

    NOT durable. See module docstring. The owner-panel WebSocket registers
    here to receive agent questions and to deliver the doctor's responses.
    """

    def __init__(self, *, timezone: str = "Asia/Kolkata"):
        self._timezone = timezone
        self._queries: list[DoctorQuery] = []
        self._events: dict[int, asyncio.Event] = {}
        self._ws_connections: list = []

    def register_ws(self, ws) -> None:
        self._ws_connections.append(ws)

    def unregister_ws(self, ws) -> None:
        if ws in self._ws_connections:
            self._ws_connections.remove(ws)

    async def ask_doctor(self, question: str, patient_context: str = "") -> DoctorQuery:
        query = DoctorQuery(
            question=question,
            from_patient=patient_context,
            timestamp=datetime.now(ZoneInfo(self._timezone)),
        )
        self._queries.append(query)
        query_id = len(self._queries) - 1
        self._events[query_id] = asyncio.Event()

        payload = {
            "type": "agent_question",
            "question": question,
            "patient_context": patient_context,
            "query_id": query_id,
        }
        for ws in list(self._ws_connections):
            try:
                await ws.send_json(payload)
            except Exception:
                # A dead socket must not break the agent's turn.
                pass

        logger.info("agent_asked_doctor query_id=%d", query_id)
        return query

    async def wait_for_response(self, query: DoctorQuery, timeout: float = 60.0) -> str | None:
        if query.answered:
            return query.response
        try:
            query_id = self._queries.index(query)
        except ValueError:
            return None
        event = self._events.get(query_id)
        if event is None:
            return None
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return query.response
        except asyncio.TimeoutError:
            logger.warning("doctor_no_response query_id=%d", query_id)
            return None

    def doctor_responds(self, query_id: int, response: str) -> bool:
        if query_id < 0 or query_id >= len(self._queries):
            return False
        query = self._queries[query_id]
        if query.answered:
            return False
        query.response = response
        query.responded_at = datetime.now(ZoneInfo(self._timezone))
        event = self._events.get(query_id)
        if event is not None:
            event.set()
        logger.info("doctor_responded query_id=%d", query_id)
        return True

    @property
    def pending_queries(self) -> list[dict]:
        return [
            {
                "id": i,
                "question": q.question,
                "patient_context": q.from_patient,
                "time": q.timestamp.strftime("%H:%M:%S"),
                "answered": q.answered,
                "response": q.response,
            }
            for i, q in enumerate(self._queries)
        ]
