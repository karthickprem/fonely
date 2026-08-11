"""Two-way bridge between agent and doctor/owner.

When the agent needs information it doesn't have (availability, special
instructions, approval), it posts a question here. The doctor sees it
in the owner panel and responds. The agent picks up the response.

This is the in-memory demo version. Production uses WhatsApp messaging.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
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


class DoctorBridge:
    """Async bridge for agent↔doctor communication."""

    def __init__(self):
        self._queries: list[DoctorQuery] = []
        self._pending_event: asyncio.Event | None = None
        self._ws_connections: list = []

    def register_ws(self, ws):
        self._ws_connections.append(ws)

    def unregister_ws(self, ws):
        if ws in self._ws_connections:
            self._ws_connections.remove(ws)

    async def ask_doctor(self, question: str, patient_context: str = "") -> DoctorQuery:
        """Agent asks the doctor a question. Returns the query object.
        Notifies all connected owner WebSockets."""
        query = DoctorQuery(
            question=question,
            from_patient=patient_context,
            timestamp=datetime.now(ZoneInfo("Asia/Kolkata")),
        )
        self._queries.append(query)
        self._pending_event = asyncio.Event()

        # Notify all connected owner panels
        msg = {
            "type": "agent_question",
            "question": question,
            "patient_context": patient_context,
            "query_id": len(self._queries) - 1,
        }
        for ws in list(self._ws_connections):
            try:
                await ws.send_json(msg)
            except Exception:
                pass

        logger.info(f"Agent asked doctor: {question}")
        return query

    async def wait_for_response(self, query: DoctorQuery, timeout: float = 60.0) -> str | None:
        """Wait for the doctor to respond. Returns response text or None on timeout."""
        if query.answered:
            return query.response

        if self._pending_event is None:
            return None

        try:
            await asyncio.wait_for(self._pending_event.wait(), timeout=timeout)
            return query.response
        except asyncio.TimeoutError:
            logger.warning("Doctor did not respond in time")
            return None

    def doctor_responds(self, query_id: int, response: str) -> bool:
        """Doctor provides a response to a query."""
        if query_id < 0 or query_id >= len(self._queries):
            return False

        query = self._queries[query_id]
        if query.answered:
            return False

        query.response = response
        query.responded_at = datetime.now(ZoneInfo("Asia/Kolkata"))

        if self._pending_event:
            self._pending_event.set()

        logger.info(f"Doctor responded: {response}")
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


# Singleton bridge
BRIDGE = DoctorBridge()
