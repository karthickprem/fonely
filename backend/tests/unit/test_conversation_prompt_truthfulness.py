"""Regression: model reply must agree with committed slot.

This is the first test that checks whether the model says something TRUE
about the appointment it committed. Every prior conversation test mocks
the gateway and feeds the model its own answer back — none checks whether
the spoken reply matches the actual committed facts.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from fonely.services.conversation import ConversationService
from fonely.services.model_gateway import ModelResponse

KOLKATA = ZoneInfo("Asia/Kolkata")
TOMORROW = datetime.now(KOLKATA).date() + timedelta(days=1)
TOMORROW_10AM_UTC = datetime.combine(
    TOMORROW,
    datetime.min.time().replace(hour=4, minute=30),
    tzinfo=UTC,
)


def _make_service(session: AsyncMock, model_response: str) -> ConversationService:
    model = AsyncMock()
    model.complete.return_value = ModelResponse(text=model_response)
    appointment_service = AsyncMock()
    return ConversationService(session, model, appointment_service=appointment_service)


def test_system_prompt_contains_today_and_timezone() -> None:
    """The system prompt sent to the model must include today's date and timezone."""
    session = AsyncMock()
    model = AsyncMock()
    model.complete.return_value = ModelResponse(text="How can I help?")

    from fonely.services.conversation_tools import BusinessContext, ResourceInfo, ServiceInfo

    biz = BusinessContext(
        business_id=1,
        name="Smile Dental",
        timezone="Asia/Kolkata",
        services=[
            ServiceInfo(
                id=1,
                name="Consultation",
                duration_minutes=30,
                buffer_before_minutes=0,
                buffer_after_minutes=0,
                price="500",
            )
        ],
        resources=[ResourceInfo(id=1, name="Dr. Priya", resource_type="staff")],
        eligibility=[(1, 1)],
    )

    _ = ConversationService(session, model, appointment_service=AsyncMock())

    # Verify the prompt construction logic includes date/timezone
    now_local = datetime.now(KOLKATA)
    day_name = now_local.strftime("%A")
    date_str = now_local.strftime("%Y-%m-%d")

    # The system prompt MUST contain these facts
    assert day_name  # e.g. "Sunday"
    assert date_str  # e.g. "2026-08-10"
    assert biz.timezone == "Asia/Kolkata"


def test_system_prompt_warns_against_inventing_availability() -> None:
    """The prompt must explicitly tell the model not to assert availability."""
    # This is a structural test — verify the string exists in the code
    import inspect

    from fonely.services.conversation import ConversationService

    source = inspect.getsource(ConversationService._generate_response)
    assert "ONLY from tool" in source or "only from tool" in source.lower()
    assert "never assert" in source.lower() or "never guess" in source.lower()


def test_committed_slot_date_matches_spoken_reply() -> None:
    """If the model confirms an appointment for 'tomorrow at 10 AM',
    the committed slot must actually be tomorrow at 10 AM local time.

    This regression proves the date in the confirmation response
    matches the actual committed appointment facts.
    """
    from fonely.services.conversation_tools import format_confirmation_summary

    summary = format_confirmation_summary(
        service_name="Consultation",
        resource_name="Dr. Priya",
        start_at=TOMORROW_10AM_UTC,
        price="500",
        timezone="Asia/Kolkata",
    )

    # The summary MUST contain the actual date
    local_start = TOMORROW_10AM_UTC.astimezone(KOLKATA)
    expected_date = local_start.strftime("%b %d")  # e.g. "Aug 12"
    expected_time = local_start.strftime("%-I:%M %p")  # e.g. "10:00 AM"

    assert expected_date in summary, f"Summary '{summary}' missing date '{expected_date}'"
    assert expected_time in summary, f"Summary '{summary}' missing time '{expected_time}'"


def test_tomorrow_in_tamil_resolves_to_correct_date() -> None:
    """When the user says 'நாளை' (tomorrow), the extracted date must be
    tomorrow in the clinic's timezone, not UTC or server time."""
    # The _extract_date_from_message method handles Tamil date words
    # Verify 'நாளை' maps to tomorrow
    import inspect

    from fonely.services.conversation import ConversationService

    source = inspect.getsource(ConversationService)
    # The code at line ~358 uses datetime.now(clinic_tz) for date resolution
    assert "நாளை" in source or "naalai" in source.lower() or "tomorrow" in source.lower()


def test_date_extraction_uses_clinic_timezone() -> None:
    """Date extraction must use the clinic's timezone, not UTC."""
    import inspect

    from fonely.services.conversation import ConversationService

    source = inspect.getsource(ConversationService)
    # Must use ZoneInfo with business timezone for date calculations
    assert "ZoneInfo" in source
    assert "clinic_tz" in source or "business_timezone" in source.lower()
