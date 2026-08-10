"""Frozen black-box acceptance contract for CI execution-evidence system.

Each test covers one required success or failure class from the approved
design. Tests are marked skip until implementation is complete.
"""

from __future__ import annotations

import pytest

PENDING = pytest.mark.skip(reason="implementation pending")


class TestSuccess:
    @PENDING
    def test_complete_success_with_pg_call_bodies(self, tmp_path):
        """All phases pass, every PG node has call-body evidence, terminal is success."""

    @PENDING
    def test_success_with_exact_pg_waiver(self, tmp_path):
        """One PG node setup-skipped with exact governed waiver, terminal is success."""


class TestPrePytestPhaseFailure:
    @PENDING
    def test_lint_failure(self, tmp_path):
        """Ordinary pre-pytest phase fails, terminal is test_failed."""

    @PENDING
    def test_dependency_install_failure(self, tmp_path):
        """Dependency install fails, terminal is test_failed."""


class TestTestFailure:
    @PENDING
    def test_non_pg_test_failure(self, tmp_path):
        """Non-PG test fails, terminal is test_failed."""

    @PENDING
    def test_pg_test_failure(self, tmp_path):
        """PG test call failure, terminal is test_failed."""


class TestPGProof:
    @PENDING
    def test_pg_setup_skip_without_waiver(self, tmp_path):
        """One PG setup-skipped without waiver, terminal is evidence_failed."""

    @PENDING
    def test_pg_skip_while_another_executes(self, tmp_path):
        """One PG skip, another PG executes — fails without per-node waiver."""

    @PENDING
    def test_all_pg_skipped(self, tmp_path):
        """Every PG node skipped, terminal is evidence_failed."""


class TestSkipAndXfail:
    @PENDING
    def test_marker_skip(self, tmp_path):
        """Marker-skipped non-PG node, terminal is evidence_failed."""

    @PENDING
    def test_skipif(self, tmp_path):
        """skipif-skipped non-PG node, terminal is evidence_failed."""

    @PENDING
    def test_setup_skip(self, tmp_path):
        """Setup-skipped non-PG node, terminal is evidence_failed."""

    @PENDING
    def test_parametrized_literal_bracket_skip(self, tmp_path):
        """Parametrized node with literal [param] skipped."""

    @PENDING
    def test_body_xfail(self, tmp_path):
        """Body xfail without waiver, terminal is evidence_failed."""

    @PENDING
    def test_pre_call_xfail(self, tmp_path):
        """Pre-call xfail without waiver, terminal is evidence_failed."""

    @PENDING
    def test_strict_xpass(self, tmp_path):
        """Strict XPASS, terminal is test_failed."""

    @PENDING
    def test_non_strict_xpass(self, tmp_path):
        """Non-strict XPASS, terminal is test_failed."""


class TestEventStreamIntegrity:
    @PENDING
    def test_duplicate_phase_event(self, tmp_path):
        """Duplicate setup/call/teardown for same node, terminal is evidence_failed."""

    @PENDING
    def test_contradictory_phase_event(self, tmp_path):
        """Contradictory outcomes for same phase, terminal is evidence_failed."""

    @PENDING
    def test_missing_event(self, tmp_path):
        """Node in manifest missing from event stream, terminal is evidence_failed."""

    @PENDING
    def test_extra_event(self, tmp_path):
        """Event for node not in manifest, terminal is evidence_failed."""

    @PENDING
    def test_cross_partition_event(self, tmp_path):
        """Event in wrong partition, terminal is evidence_failed."""

    @PENDING
    def test_sequence_gap(self, tmp_path):
        """Sequence gap in event stream, terminal is evidence_failed."""

    @PENDING
    def test_sequence_duplicate(self, tmp_path):
        """Duplicate sequence number, terminal is evidence_failed."""

    @PENDING
    def test_truncated_event_line(self, tmp_path):
        """Truncated JSONL line, terminal is evidence_failed."""

    @PENDING
    def test_missing_final_record(self, tmp_path):
        """No final partition record, terminal is incomplete."""

    @PENDING
    def test_digest_mismatch(self, tmp_path):
        """Event stream digest does not match final record, terminal is evidence_failed."""

    @PENDING
    def test_run_identity_mismatch(self, tmp_path):
        """Event stream run identity differs from manifest, terminal is evidence_failed."""


class TestWaiverGovernance:
    @PENDING
    def test_waiver_wrong_schema(self, tmp_path):
        """Wrong waiver schema version, terminal is evidence_failed."""

    @PENDING
    def test_waiver_wrong_environment(self, tmp_path):
        """Waiver for wrong environment, terminal is evidence_failed."""

    @PENDING
    def test_waiver_invalid_url(self, tmp_path):
        """Waiver with invalid issue URL, terminal is evidence_failed."""

    @PENDING
    def test_waiver_expired(self, tmp_path):
        """Expired waiver for current environment, terminal is evidence_failed."""

    @PENDING
    def test_waiver_expired_other_env(self, tmp_path):
        """Expired waiver for another environment fails globally."""

    @PENDING
    def test_waiver_future_created(self, tmp_path):
        """Future-created waiver, terminal is evidence_failed."""

    @PENDING
    def test_waiver_lifetime_exceeded(self, tmp_path):
        """Waiver lifetime >14 days, terminal is evidence_failed."""

    @PENDING
    def test_waiver_duplicate_coverage(self, tmp_path):
        """Duplicate node/environment waiver coverage, terminal is evidence_failed."""

    @PENDING
    def test_waiver_unused_current_env(self, tmp_path):
        """Unused waiver for current environment, terminal is evidence_failed."""

    @PENDING
    def test_waiver_non_pg_rejected(self, tmp_path):
        """Waiver for non-PG node rejected."""

    @PENDING
    def test_waiver_wrong_exception_class(self, tmp_path):
        """Waiver with wrong exception class, terminal is evidence_failed."""


class TestJUnitDiagnostic:
    @PENDING
    def test_malformed_junit(self, tmp_path):
        """Malformed JUnit XML, terminal is evidence_failed."""

    @PENDING
    def test_truncated_junit(self, tmp_path):
        """Truncated JUnit XML, terminal is evidence_failed."""

    @PENDING
    def test_junit_failure_contradiction(self, tmp_path):
        """JUnit failure count contradicts canonical events, terminal is evidence_failed."""


class TestTerminalEvidence:
    @PENDING
    def test_missing_terminal(self, tmp_path):
        """No terminal.json exists, workflow fails."""

    @PENDING
    def test_incomplete_terminal(self, tmp_path):
        """Terminal state is incomplete."""

    @PENDING
    def test_double_finalization(self, tmp_path):
        """Second terminal creation fails."""

    @PENDING
    def test_completeness_hash_mismatch(self, tmp_path):
        """Terminal artifact hash doesn't match actual file, terminal is evidence_failed."""

    @PENDING
    def test_completeness_sha_mismatch(self, tmp_path):
        """Terminal source SHA doesn't match manifest, terminal is evidence_failed."""


class TestFileSafety:
    @PENDING
    def test_live_final_symlink(self, tmp_path):
        """Live symlink at evidence path rejected."""

    @PENDING
    def test_dangling_final_symlink(self, tmp_path):
        """Dangling symlink at evidence path rejected."""

    @PENDING
    def test_temp_symlink_attack(self, tmp_path):
        """Temporary file replaced with symlink during write rejected."""

    @PENDING
    def test_path_traversal(self, tmp_path):
        """Path traversal outside evidence root rejected."""


class TestUpload:
    @PENDING
    def test_upload_failure_preserves_terminal(self, tmp_path):
        """Upload transport failure does not alter terminal record."""
