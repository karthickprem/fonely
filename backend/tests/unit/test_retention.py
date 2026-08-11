"""Unit tests for data retention policies and PII audit logging."""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from fonely.core.pii_audit import log_pii_access
from fonely.core.retention import get_retention_policies


class TestRetentionPolicies:
    def test_default_conversation_retention(self) -> None:
        policies = get_retention_policies()
        assert policies["conversations"].retention_days == 90

    def test_default_appointment_retention(self) -> None:
        policies = get_retention_policies()
        assert policies["appointments"].retention_days == 365

    def test_default_notification_retention(self) -> None:
        policies = get_retention_policies()
        assert policies["notifications_delivered"].retention_days == 30

    def test_default_dead_letter_retention(self) -> None:
        policies = get_retention_policies()
        assert policies["notifications_dead_letter"].retention_days == 90

    def test_env_override_conversations(self) -> None:
        with patch.dict(os.environ, {"RETENTION_CONVERSATIONS_DAYS": "30"}):
            policies = get_retention_policies()
        assert policies["conversations"].retention_days == 30

    def test_env_override_appointments(self) -> None:
        with patch.dict(os.environ, {"RETENTION_APPOINTMENTS_DAYS": "180"}):
            policies = get_retention_policies()
        assert policies["appointments"].retention_days == 180

    def test_invalid_env_uses_default(self) -> None:
        with patch.dict(os.environ, {"RETENTION_CONVERSATIONS_DAYS": "abc"}):
            policies = get_retention_policies()
        assert policies["conversations"].retention_days == 90

    def test_zero_env_uses_default(self) -> None:
        with patch.dict(os.environ, {"RETENTION_CONVERSATIONS_DAYS": "0"}):
            policies = get_retention_policies()
        assert policies["conversations"].retention_days == 90

    def test_all_policies_have_descriptions(self) -> None:
        for policy in get_retention_policies().values():
            assert policy.description

    def test_default_call_transcript_retention(self) -> None:
        policies = get_retention_policies()
        assert policies["call_transcripts"].retention_days == 90

    def test_env_override_call_transcripts(self) -> None:
        with patch.dict(os.environ, {"RETENTION_CALL_TRANSCRIPTS_DAYS": "30"}):
            policies = get_retention_policies()
        assert policies["call_transcripts"].retention_days == 30

    def test_call_transcripts_do_not_outlive_conversations(self) -> None:
        """The same words reach us by voice and by WhatsApp.

        If the voice copy were kept longer than the chat copy, the retention
        promise would depend on which channel the patient happened to use,
        which is not a promise.
        """
        policies = get_retention_policies()
        assert (
            policies["call_transcripts"].retention_days <= policies["conversations"].retention_days
        )

    def test_turns_match_conversations(self) -> None:
        policies = get_retention_policies()
        assert (
            policies["conversation_turns"].retention_days
            == policies["conversations"].retention_days
        )


class TestPIIAuditLogging:
    def test_log_emits_correct_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="fonely.pii_audit"):
            log_pii_access(
                operation="read",
                data_type="appointment",
                business_id=1,
                accessor="api:internal",
                record_count=3,
                correlation_id="abc-123",
            )
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.operation == "read"  # type: ignore[attr-defined]
        assert record.data_type == "appointment"  # type: ignore[attr-defined]
        assert record.business_id == 1  # type: ignore[attr-defined]
        assert record.accessor == "api:internal"  # type: ignore[attr-defined]
        assert record.record_count == 3  # type: ignore[attr-defined]
        assert record.correlation_id == "abc-123"  # type: ignore[attr-defined]

    def test_log_does_not_contain_phone_or_name(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="fonely.pii_audit"):
            log_pii_access(
                operation="search",
                data_type="conversation",
                business_id=2,
                accessor="api:whatsapp",
                record_count=1,
            )
        output = caplog.text
        assert "+91" not in output
        assert "patient" not in output.lower()

    def test_log_without_correlation_id(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="fonely.pii_audit"):
            log_pii_access(
                operation="read",
                data_type="notification",
                business_id=1,
                accessor="worker:notification",
                record_count=5,
            )
        assert caplog.records[0].correlation_id is None  # type: ignore[attr-defined]


class TestRetentionResult:
    def test_to_dict(self) -> None:
        from fonely.services.data_retention import RetentionResult

        result = RetentionResult(
            conversations_deleted=5,
            turns_deleted=12,
            notifications_deleted=3,
            pending_actions_deleted=1,
            call_transcripts_redacted=7,
            execution_time_ms=45.6,
        )
        d = result.to_dict()
        assert d["conversations_deleted"] == 5
        assert d["turns_deleted"] == 12
        assert d["notifications_deleted"] == 3
        assert d["pending_actions_deleted"] == 1
        assert d["call_transcripts_redacted"] == 7
        assert d["execution_time_ms"] == 45.6
