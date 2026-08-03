"""Unit tests for cancel and reschedule conversation flows."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from fonely.domain.conversation.safety import classify_intent
from fonely.domain.conversation.state import (
    ConversationIntent,
    ConversationState,
)
from fonely.services.conversation import _CONVERSATIONS, ConversationService
from fonely.services.conversation_tools import (
    PatientAppointment,
    parse_appointment_selection,
)
from fonely.services.model_gateway import ModelResponse


@pytest.fixture(autouse=True)
def _clear():
    _CONVERSATIONS.clear()
    yield
    _CONVERSATIONS.clear()


@pytest.fixture(autouse=True)
def _skip_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(self: object, conversation_id: str, turn: object) -> None:
        pass

    monkeypatch.setattr(ConversationService, "_persist_turn", _noop)


def _mock_gateway() -> AsyncMock:
    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text="Sure!")
    return gw


def _sample_appointment(
    appointment_id: int = 1,
    service_name: str = "General Consultation",
    resource_name: str = "Dr. Priya",
    version: int = 1,
) -> PatientAppointment:
    from datetime import UTC, datetime

    return PatientAppointment(
        appointment_id=appointment_id,
        service_name=service_name,
        resource_name=resource_name,
        start_at=datetime(2026, 8, 5, 10, 30, tzinfo=UTC),
        price="300",
        status="confirmed",
        pending_action_id=100 + appointment_id,
        version=version,
        service_id=appointment_id,
        resource_id=appointment_id,
    )


class TestCancelIntentDetection:
    def test_english_cancel_appointment(self):
        result = classify_intent("cancel my appointment")
        assert result.intent == ConversationIntent.CANCEL_APPOINTMENT

    def test_english_cancel_booking(self):
        result = classify_intent("I want to cancel the appointment")
        assert result.intent == ConversationIntent.CANCEL_APPOINTMENT

    def test_tamil_cancel(self):
        result = classify_intent("appointment cancel பண்ணுங்க")
        assert result.intent == ConversationIntent.CANCEL_APPOINTMENT

    def test_tanglish_cancel(self):
        result = classify_intent("appointment-a cancel pannanum")
        assert result.intent == ConversationIntent.CANCEL_APPOINTMENT

    def test_dont_want_appointment(self):
        result = classify_intent("I don't want the appointment")
        assert result.intent == ConversationIntent.CANCEL_APPOINTMENT


class TestRescheduleIntentDetection:
    def test_english_reschedule(self):
        result = classify_intent("reschedule my appointment")
        assert result.intent == ConversationIntent.RESCHEDULE

    def test_english_change_appointment(self):
        result = classify_intent("change my appointment time")
        assert result.intent == ConversationIntent.RESCHEDULE

    def test_english_postpone(self):
        result = classify_intent("postpone the appointment")
        assert result.intent == ConversationIntent.RESCHEDULE

    def test_tanglish_change(self):
        result = classify_intent("appointment-a change pannanum")
        assert result.intent == ConversationIntent.RESCHEDULE


class TestAppointmentSelection:
    def test_select_by_number(self):
        appts = [_sample_appointment(1), _sample_appointment(2, "Scaling", "Dr. Arjun")]
        result = parse_appointment_selection("1", appts)
        assert result is not None
        assert result.appointment_id == 1

    def test_select_by_second_number(self):
        appts = [_sample_appointment(1), _sample_appointment(2, "Scaling", "Dr. Arjun")]
        result = parse_appointment_selection("2", appts)
        assert result is not None
        assert result.appointment_id == 2

    def test_select_by_service_name(self):
        appts = [_sample_appointment(1), _sample_appointment(2, "Scaling", "Dr. Arjun")]
        result = parse_appointment_selection("scaling", appts)
        assert result is not None
        assert result.appointment_id == 2

    def test_select_by_doctor_name(self):
        appts = [_sample_appointment(1), _sample_appointment(2, "Scaling", "Dr. Arjun")]
        result = parse_appointment_selection("Priya", appts)
        assert result is not None
        assert result.appointment_id == 1

    def test_ambiguous_returns_none(self):
        appts = [_sample_appointment(1)]
        result = parse_appointment_selection("something random", appts)
        assert result is None

    def test_out_of_range_number(self):
        appts = [_sample_appointment(1)]
        result = parse_appointment_selection("5", appts)
        assert result is None


class TestCancelFlowNoAppointments:
    @pytest.mark.asyncio
    async def test_no_appointments_ends_conversation(self):
        from fonely.domain.pending_actions.commands import ActorContext
        from fonely.models.enums import CallerRole

        session = AsyncMock()
        empty_result = MagicMock(scalars=lambda: MagicMock(all=lambda: []))
        empty_result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=empty_result)
        nested = AsyncMock()
        nested.__aenter__ = AsyncMock(return_value=None)
        nested.__aexit__ = AsyncMock(return_value=False)
        session.begin_nested = MagicMock(return_value=nested)
        session.add = MagicMock()
        session.get = AsyncMock(return_value=None)
        session.commit = AsyncMock()

        gateway = _mock_gateway()
        appt_service = MagicMock()

        service = ConversationService(session, gateway, appointment_service=appt_service)
        actor = ActorContext(
            business_id=1,
            normalized_phone="+919123456789",
            verified_role=CallerRole.CUSTOMER,
        )

        turn = await service.process_message("cancel-test", 1, actor, "cancel my appointment")
        assert "no upcoming appointments" in turn.assistant_response.lower() or (
            turn.state == ConversationState.ENDED
        )


class TestRescheduleFlowNoAppointments:
    @pytest.mark.asyncio
    async def test_no_appointments_ends_conversation(self):
        from fonely.domain.pending_actions.commands import ActorContext
        from fonely.models.enums import CallerRole

        session = AsyncMock()
        empty_result = MagicMock(scalars=lambda: MagicMock(all=lambda: []))
        empty_result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=empty_result)
        nested = AsyncMock()
        nested.__aenter__ = AsyncMock(return_value=None)
        nested.__aexit__ = AsyncMock(return_value=False)
        session.begin_nested = MagicMock(return_value=nested)
        session.add = MagicMock()
        session.get = AsyncMock(return_value=None)
        session.commit = AsyncMock()

        gateway = _mock_gateway()
        appt_service = MagicMock()

        service = ConversationService(session, gateway, appointment_service=appt_service)
        actor = ActorContext(
            business_id=1,
            normalized_phone="+919123456789",
            verified_role=CallerRole.CUSTOMER,
        )

        turn = await service.process_message(
            "reschedule-test", 1, actor, "reschedule my appointment"
        )
        assert "no upcoming appointments" in turn.assistant_response.lower() or (
            turn.state == ConversationState.ENDED
        )
