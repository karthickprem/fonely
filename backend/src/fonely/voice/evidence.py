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
port. The runtime is handed a writer; in tests that is ``FakeEvidenceWriter``,
and once the columns land the application wires ``SqlDpdpEvidenceWriter``.

The content digest is a stable hash of exactly the notice text, version, and
locale — the three facts the CHECK pairs with ``completed_at``. It is computed
here so the same value is written regardless of writer implementation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

# Unit separator between digest components: a byte that cannot appear in the
# text/version/locale, so "1|en" + "x" and "1" + "en|x" can never collide.
_SEP = "\x1f"


def notice_content_digest(text: str, version: str, locale: str) -> str:
    """Stable sha256 hex of the notice's identifying facts.

    Order-fixed and separator-delimited so no two distinct (version, locale,
    text) triples share a digest. This is the immutable value the compliance
    CHECK pairs with completed_at — it changes iff the spoken notice changes.
    """
    payload = f"{version}{_SEP}{locale}{_SEP}{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


@dataclass
class SqlDpdpEvidenceWriter:
    """Writes evidence to the call record's DPDP columns via the injected async
    session factory. Ships UN-WIRED: the application only constructs this once
    the admission lane's migration has added the columns + null-safe CHECK.

    The UPDATE sets all four columns in one statement so the CHECK sees a
    complete row; the DB constraint — not this code — is the guarantee that
    partial evidence cannot exist.
    """

    session_factory: object  # Callable[[], AbstractAsyncContextManager[AsyncSession]]

    async def write(
        self,
        *,
        call_id: int,
        completed_at: datetime,
        notice_version: str,
        locale: str,
        content_digest: str,
    ) -> None:
        from sqlalchemy import text as sql_text

        async with self.session_factory() as session:  # type: ignore[operator]
            await session.execute(
                sql_text(
                    "UPDATE calls SET "
                    "dpdp_completed_at = :completed_at, "
                    "dpdp_notice_version = :notice_version, "
                    "dpdp_locale = :locale, "
                    "dpdp_content_digest = :content_digest "
                    "WHERE id = :call_id"
                ),
                {
                    "completed_at": completed_at,
                    "notice_version": notice_version,
                    "locale": locale,
                    "content_digest": content_digest,
                    "call_id": call_id,
                },
            )
            await session.commit()
