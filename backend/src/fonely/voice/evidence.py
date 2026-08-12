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

``SqlDpdpEvidenceWriter`` is the real writer. It targets the CEO #31 columns on
``calls`` — ``dpdp_notice_completed_at`` (timestamptz), ``dpdp_notice_version``
(varchar10), ``dpdp_notice_locale`` (varchar10), and
``dpdp_notice_content_digest`` (varchar64, lowercase sha256) — all nullable
under a ``num_nonnulls(...) IN (0, 4)`` CHECK (either none or all four, so
partial evidence is unrepresentable) plus a digest regex CHECK. Those columns
are on disk at revision 0018. The writer landed in the same commit as the
PostgreSQL test that inserts a real call, runs it, and asserts the persisted row
including the digest, so it has never existed as un-exercised production code.

READ THIS BEFORE CONCLUDING DPDP IS HANDLED. A working writer is not a spoken
notice. Nothing on the production path calls it yet, and the reason is bigger
than this module: no voice runtime is mounted at all. ``api/channels/exotel.py``
looks up ``app.state.voice_audio_runtime``, nothing in ``src/`` ever assigns it,
so every admitted call closes 1011 before a word is synthesized. The ordered
sequence in ``open_order.py`` that would call this writer is itself reachable
only from its unit test. So: the columns exist, the writer works and is proven
against them, and still no patient has been read a notice and no evidence row
has ever been written outside a test. The columns existing is not the guarantee.
The writer existing is not the guarantee. A patient hearing the notice, on a
call a mounted runtime actually served, is the guarantee — tracked as CEO #31
(evidence) and #38 (the unmounted runtime), and not claimed here.

The content digest is a stable hash of exactly the notice text, version, and
locale — the three facts the CHECK pairs with ``completed_at``. It is computed
here so the same value is written regardless of writer implementation.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("fonely.voice.evidence")

# Same shape as backend_ports.SessionFactory, defined locally rather than
# imported: that module pulls in the whole command-port graph, and this one is a
# leaf that the pure open-sequence orchestration depends on. A structural alias
# duplicated in two places cannot silently diverge -- mypy checks both against
# the same injected factory at the call site.
SessionFactory = Callable[[], "AbstractAsyncContextManager[AsyncSession]"]


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


class DpdpEvidenceWriteError(RuntimeError):
    """The evidence could not be recorded, so consent is not provable.

    Raised rather than returned because the open sequence keeps STT closed on
    any exception from ``write``. A distinct type so a log or a test can tell
    "no such call" from "a different notice is already recorded" from a
    database error, instead of matching on message text.
    """


class SqlDpdpEvidenceWriter:
    """Production writer: the four ``dpdp_notice_*`` columns on ``calls``.

    Owns its own transaction and commits before returning. This is the point of
    the whole module, so it is worth being explicit: the guarantee is that the
    evidence is DURABLE at the instant STT opens. If this writer joined the
    caller's long-lived session instead, ``write`` would return, the latch would
    open, the patient would start speaking, and the evidence would still be
    sitting uncommitted in a transaction that a crash or a rollback later in the
    call would discard — leaving captured speech with no record of the notice
    that permitted capturing it. A short transaction that commits here is the
    only shape that makes the ordering claim true.

    Validation is split on a deliberate line: this class checks only what the
    database physically cannot, and lets the CHECK constraints enforce the rest.
    A malformed digest is rejected by ``ck_calls_dpdp_notice_digest_hex`` and a
    partial write by ``ck_calls_dpdp_notice_all_or_none``, so re-checking them
    here would add a second copy of a rule that can drift from the schema. A
    naive ``completed_at`` is the exception: ``timestamptz`` would silently
    coerce it using the session TimeZone, recording a real timestamp that is
    simply wrong by hours, with no error anywhere. The database cannot catch
    that one, so this class does.
    """

    def __init__(self, *, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def write(
        self,
        *,
        call_id: int,
        completed_at: datetime,
        notice_version: str,
        locale: str,
        content_digest: str,
    ) -> None:
        if completed_at.tzinfo is None or completed_at.utcoffset() is None:
            raise DpdpEvidenceWriteError(
                f"naive completed_at for call {call_id}: timestamptz would coerce it silently"
            )

        params = {
            "call_id": call_id,
            "completed_at": completed_at,
            "version": notice_version,
            "locale": locale,
            "digest": content_digest,
        }

        async with self._session_factory() as session:
            # `AND dpdp_notice_completed_at IS NULL` makes this a claim on
            # unrecorded evidence rather than a blind overwrite. Two concurrent
            # writers for one call cannot both win, and a late duplicate cannot
            # move the recorded completion time to a later instant than the one
            # the patient actually heard the notice at.
            result = await session.execute(
                text(
                    "UPDATE calls SET "
                    "  dpdp_notice_completed_at = :completed_at, "
                    "  dpdp_notice_version = :version, "
                    "  dpdp_notice_locale = :locale, "
                    "  dpdp_notice_content_digest = :digest "
                    "WHERE id = :call_id AND dpdp_notice_completed_at IS NULL"
                ),
                params,
            )
            if result.rowcount == 1:  # type: ignore[attr-defined]
                await session.commit()
                logger.info(
                    "dpdp_notice_evidence_written",
                    extra={
                        "call_id": call_id,
                        "notice_version": notice_version,
                        "locale": locale,
                    },
                )
                return

            # Zero rows updated is ambiguous on its own -- no such call, or
            # evidence already present -- and the two need opposite outcomes, so
            # read the row rather than guess.
            existing = await session.execute(
                text(
                    "SELECT dpdp_notice_content_digest, dpdp_notice_version, "
                    "       dpdp_notice_locale "
                    "FROM calls WHERE id = :call_id"
                ),
                {"call_id": call_id},
            )
            row = existing.one_or_none()

        if row is None:
            raise DpdpEvidenceWriteError(f"no call row {call_id} to record notice evidence against")

        if (row[0], row[1], row[2]) == (content_digest, notice_version, locale):
            # A retry whose first attempt did commit before its response was
            # lost. Identical evidence is already durable, which is exactly what
            # this call was asking for, so it succeeds -- and deliberately does
            # NOT rewrite completed_at, because the first completion is the one
            # the patient experienced.
            logger.info(
                "dpdp_notice_evidence_already_recorded",
                extra={"call_id": call_id, "notice_version": notice_version},
            )
            return

        # Different notice already recorded. Overwriting would replace a true
        # statement about what this patient heard with a different one, so this
        # fails and the latch stays closed.
        raise DpdpEvidenceWriteError(
            f"call {call_id} already has different notice evidence recorded "
            f"(stored version {row[1]!r} locale {row[2]!r}, refusing to overwrite)"
        )
