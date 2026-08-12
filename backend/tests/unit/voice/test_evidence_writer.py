"""DPDP evidence writer + content digest (V-lane step 1).

Proves the digest is stable and discriminating, the fake writer records
all-or-nothing, and the failure path raises (so the runtime can keep STT
closed on a failed evidence write).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fonely.voice.evidence import (
    FakeEvidenceWriter,
    notice_content_digest,
)


class TestNoticeContentDigest:
    def test_deterministic(self):
        a = notice_content_digest("hello", "1", "ta-IN")
        b = notice_content_digest("hello", "1", "ta-IN")
        assert a == b
        assert len(a) == 64  # sha256 hex

    def test_digest_matches_column_constraint_lowercase_hex64(self):
        # The CEO #31 dpdp_notice_content_digest column is varchar64 lowercase
        # sha256 with a regex CHECK; the digest we write must satisfy it.
        import re

        d = notice_content_digest("வணக்கம் notice text", "1", "ta-IN")
        assert re.fullmatch(r"[0-9a-f]{64}", d) is not None

    def test_changes_with_text(self):
        assert notice_content_digest("hello", "1", "ta-IN") != notice_content_digest(
            "goodbye", "1", "ta-IN"
        )

    def test_changes_with_version(self):
        assert notice_content_digest("hello", "1", "ta-IN") != notice_content_digest(
            "hello", "2", "ta-IN"
        )

    def test_changes_with_locale(self):
        assert notice_content_digest("hello", "1", "ta-IN") != notice_content_digest(
            "hello", "1", "en-IN"
        )

    def test_length_prefix_prevents_field_collision(self):
        # Length-prefixing guarantees no boundary ambiguity: ("x","1","en") and
        # ("","1","enx") map to distinct byte streams and thus distinct digests,
        # regardless of any separator convention.
        assert notice_content_digest("x", "1", "en") != notice_content_digest("", "1", "enx")

    def test_field_containing_control_bytes_does_not_collide(self):
        # Adversarial: a field that CONTAINS the bytes a naive separator scheme
        # would use must not create a collision. Length-prefixing is immune.
        a = notice_content_digest("hello", "1", "ta\x00IN")
        b = notice_content_digest("hello", "1\x00ta", "IN")
        assert a != b


class TestFakeEvidenceWriter:
    @pytest.mark.asyncio
    async def test_records_write(self):
        w = FakeEvidenceWriter()
        ts = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
        await w.write(
            call_id=7,
            completed_at=ts,
            notice_version="1",
            locale="ta-IN",
            content_digest="abc",
        )
        assert len(w.writes) == 1
        rec = w.writes[0]
        assert rec["call_id"] == 7
        assert rec["completed_at"] == ts
        assert rec["notice_version"] == "1"
        assert rec["locale"] == "ta-IN"
        assert rec["content_digest"] == "abc"

    @pytest.mark.asyncio
    async def test_failure_raises_and_records_nothing(self):
        w = FakeEvidenceWriter(fail=True)
        with pytest.raises(RuntimeError):
            await w.write(
                call_id=7,
                completed_at=datetime(2026, 8, 12, tzinfo=UTC),
                notice_version="1",
                locale="ta-IN",
                content_digest="abc",
            )
        assert w.writes == []
