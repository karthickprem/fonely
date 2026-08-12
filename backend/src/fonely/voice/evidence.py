"""DPDP notice-completion evidence: a typed write seam, not a transcript blob.

The compliance requirement is that "this patient heard notice v<N> in <locale>
on <date>" is provable per call, as durable structured evidence — NOT inferred
from a transcript substring. The physical storage is explicit columns on the
call record (``completed_at``, ``notice_version``, ``locale``, an immutable
content digest) with a paired null-safe CHECK so partial evidence is
unrepresentable; NULL means "not completed". Those columns and their migration
are owned by the admission lane.

This module gives the voice runtime a way to WRITE that evidence without
depending on the columns existing yet: it talks to a ``DpdpEvidenceWriter``
port. The runtime is handed a writer; in tests that is ``FakeEvidenceWriter``.

The real SQL-backed writer is deliberately NOT in this module yet. It targets
the CEO #31 columns on ``calls`` — ``dpdp_notice_completed_at`` (timestamptz),
``dpdp_notice_version`` (varchar10), ``dpdp_notice_locale`` (varchar10), and
``dpdp_notice_content_digest`` (varchar64, lowercase sha256) — all nullable
under a ``num_nonnulls(...) IN (0, 4)`` CHECK (either none or all four, so
partial evidence is unrepresentable) plus a digest regex CHECK. Those columns
are NOT on disk yet (disk migration head is 0017; the DPDP migration is
authored separately and derives the next revision at authoring time). No test
could drive the SQL writer through its real path until they exist, and un-wired
production code that nothing exercises reads as done while its first real run
would be in front of a patient. The SQL writer lands in the SAME commit as the
migration and the PostgreSQL test that inserts a real call, runs the writer, and
asserts the persisted row (including the content digest) against the real
columns — and migration/writer ownership is coordinated with the CEO single
directive, not raced here. Until then only ``FakeEvidenceWriter`` exists.

The content digest is a stable hash of exactly the notice text, version, and
locale — the three facts the CHECK pairs with ``completed_at``. It is computed
here so the same value is written regardless of writer implementation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


def notice_content_digest(text: str, version: str, locale: str) -> str:
    """Stable sha256 hex of the notice's identifying facts.

    ``text`` MUST be the exact, byte-for-byte notice the patient was read — not
    a template, id, or re-rendered form. If it were normalized, the digest would
    prove we stored *something*, not that we read the patient *that thing*, and
    the evidence trail would be decorative.

    The three fields are LENGTH-PREFIXED (utf-8 byte length + NUL + bytes) rather
    than joined by a separator: a separator is only unambiguous if it can never
    occur inside a field, and we cannot guarantee that for an arbitrary locale or
    version string. Length-prefixing is unconditionally collision-free — no two
    distinct (version, locale, text) triples can produce the same byte stream —
    so the digest changes iff the spoken notice truly changes.
    """
    parts = (version, locale, text)
    payload = b"".join(f"{len(b := p.encode('utf-8'))}\x00".encode() + b for p in parts)
    return hashlib.sha256(payload).hexdigest()


class DpdpEvidenceWriter(Protocol):
    """Persists proof that a specific call heard a specific notice version.

    Implementations MUST be all-or-nothing: either all four facts land together
    or the write fails and nothing is recorded (the CHECK forbids partial rows).
    A failed write must raise — the runtime keeps STT closed on failure, so a
    silent no-op here would let capture start without provable consent.
    """

    async def write(
        self,
        *,
        call_id: int,
        completed_at: datetime,
        notice_version: str,
        locale: str,
        content_digest: str,
    ) -> None: ...


@dataclass
class FakeEvidenceWriter:
    """In-memory evidence writer for tests. Records each write; ``fail=True``
    makes ``write`` raise, to exercise the evidence-persistence-failure path
    (playback succeeded but the write failed → STT must stay closed)."""

    fail: bool = False
    writes: list[dict[str, object]] = field(default_factory=list)

    async def write(
        self,
        *,
        call_id: int,
        completed_at: datetime,
        notice_version: str,
        locale: str,
        content_digest: str,
    ) -> None:
        if self.fail:
            raise RuntimeError("evidence_write_failed")
        self.writes.append(
            {
                "call_id": call_id,
                "completed_at": completed_at,
                "notice_version": notice_version,
                "locale": locale,
                "content_digest": content_digest,
            }
        )


# The SQL-backed DpdpEvidenceWriter is intentionally absent until the C5 columns
# land on disk and the PostgreSQL proof step introduces it together with the test
# that drives it through its real path against the real columns (asserting the
# persisted notice_content_digest). Shipping it here now — un-wired, with no test
# through its real path — would be dead code that reads as done.
