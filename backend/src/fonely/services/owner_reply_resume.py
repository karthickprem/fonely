"""#43 P2 — resolve an owner's WhatsApp reply against an outstanding voice-call wait.

When a voice call gives up (needs the owner to answer, e.g. "is Dr. Priya free at
6:30?"), it leaves a DURABLE marker: a ``pending_actions`` row
(``action_type='awaiting_owner_reply'``, status ``AWAITING_OWNER_REPLY``) carrying
``call_id``/``query_type``/``correlation_code`` (Dev4's 0021 columns) and a payload
awaiting an answer. The inbound WhatsApp worker is a SEPARATE process from the voice
runtime, so when the owner replies over WhatsApp it resolves the answer through this
DB-durable marker and signals the voice side by CAS-ing the shared status to
``RESUME_REQUESTED`` (which the voice runtime's polling claim loop observes). This
service is that resolution; it never touches the in-memory ResumeRegistry.

Cardinality gate (frozen amendment — NOT "pick the oldest"):
  * 0 selectable markers   -> FallThrough (a genuine owner command).
  * exactly 1              -> resolve it (no code needed).
  * >1                     -> require a correlation code that selects EXACTLY ONE
                              of this owner's markers; otherwise resume none and
                              send a disambiguation reminder listing the codes.
Selectable == status AWAITING_OWNER_REPLY only, tenant-scoped, unexpired. A
RESUME_REQUESTED marker is already answered/claimed -> a coded reply to it is a
stale no-op reminder, never re-CAS'd.

Trust: business_id from the trusted claimed event, owner_phone from the trusted
actor (already _is_owner-verified). call_id/query_type come from the durable
marker only; ONLY the correlation-code token is parsed from the message text — the
code is correlation-only, never authentication and never a call_id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from fonely.domain.pending_actions.payloads import (
    OWNER_REPLY_CODE_ALPHABET,
    OWNER_REPLY_CODE_LENGTH,
    validate_payload,
)
from fonely.domain.pending_actions.snapshots import (
    awaiting_owner_reply_payload_digest,
    canonical_payload_dict,
)
from fonely.models.enums import PendingActionStatus, PendingActionType
from fonely.models.schema import PendingAction
from fonely.repositories.owner_reply_markers import OwnerReplyMarkerRepository
from fonely.repositories.pending_actions import PendingActionRepository

# Rolling code-guess window + bound (DB-durable per business_id, owner_phone).
_GUESS_WINDOW = timedelta(minutes=10)
_GUESS_MAX = 5
# Correlation code format: the SHARED constants Dev4's P1 generator uses, imported
# (never a local copy) so the parser can never drift from generation. Parsing is
# STRICTLY ANCHORED to avoid false positives — an English answer like "yes she is
# free" must NOT be read as a code. A code is recognized ONLY when:
#   (a) the ENTIRE trimmed message is exactly a code token, or
#   (b) it appears with an EXPLICIT delimiter: "code: XXXX" / "code #XXXX", or a
#       fully bracketed "[XXXX]" token.
# A bare code-shaped word sitting inside prose is deliberately NOT matched. The
# code is exactly OWNER_REPLY_CODE_LENGTH chars from OWNER_REPLY_CODE_ALPHABET.
_CODE_CHARS = rf"[{OWNER_REPLY_CODE_ALPHABET}]{{{OWNER_REPLY_CODE_LENGTH}}}"
_CODE_WHOLE_RE = re.compile(rf"^{_CODE_CHARS}$", re.IGNORECASE)
_CODE_LABELLED_RE = re.compile(rf"\bcode\b\s*[:#]?\s*({_CODE_CHARS})\b", re.IGNORECASE)
_CODE_BRACKETED_RE = re.compile(rf"\[\s*({_CODE_CHARS})\s*\]")
_ANSWER_MAX = 500


@dataclass(frozen=True)
class Resumed:
    ack_text: str


@dataclass(frozen=True)
class DisambiguationReminder:
    text: str


class FallThrough:
    """Sentinel: not a resume reply — let OwnerCommandService handle it."""


ResumeResolution = Resumed | DisambiguationReminder | FallThrough


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_code(message_text: str) -> str | None:
    """Extract a normalized correlation code, or None if the text carries none.

    STRICTLY anchored (see the regex block): the whole trimmed message is a code,
    OR an explicit ``code: XXXX`` / ``[XXXX]`` delimiter is present. A code-shaped
    word inside prose (e.g. "free") is NOT matched, so plain-English answers to the
    single-marker case are never mis-read as a (wrong) code. Uppercased to match
    Dev4's normalization.
    """
    text = (message_text or "").strip()
    if not text:
        return None
    if _CODE_WHOLE_RE.fullmatch(text):
        return text.upper()
    m = _CODE_LABELLED_RE.search(text) or _CODE_BRACKETED_RE.search(text)
    return m.group(1).upper() if m else None


class OwnerReplyResumeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._markers = OwnerReplyMarkerRepository(session)
        self._pending = PendingActionRepository(session)

    async def try_resolve_owner_reply(
        self, *, business_id: int, owner_phone: str, message_text: str
    ) -> ResumeResolution:
        now = _now()
        code = _parse_code(message_text)

        # A CODED reply is unambiguously resume-looking: it must NEVER fall through
        # to a generic owner command / availability mutation, even if the matching
        # marker was just resolved by a racing reply (active==0). It either resolves
        # its exact marker or gets a reminder (wrong/stale/expired code).
        if code is not None:
            marker = await self._markers.get_by_code(business_id, code, now=now)
            if marker is None:
                # Well-formed code that maps to no selectable marker: wrong /
                # expired / already-resumed / foreign. Count the miss (DB-durable
                # brute-force guard) and remind — do NOT fall through.
                attempts, maximum = await self._markers.register_wrong_code_attempt(
                    business_id=business_id,
                    owner_phone=owner_phone,
                    now=now,
                    window=_GUESS_WINDOW,
                    default_max_attempts=_GUESS_MAX,
                )
                if attempts > maximum:
                    return DisambiguationReminder(
                        "Too many incorrect codes. Please wait a few minutes and "
                        "try again with the exact code from the message."
                    )
                return DisambiguationReminder(
                    "That code doesn't match a pending question. Please reply with "
                    "the exact code shown in the message."
                )
            return await self._resolve(marker, owner_phone, message_text, code, now)

        # No code supplied — the cardinality gate over the selectable markers.
        active = await self._markers.count_awaiting(business_id, now=now)
        if active == 0:
            # No outstanding wait — this is an ordinary owner command.
            return FallThrough()
        if active == 1:
            marker = await self._markers.get_sole_awaiting(business_id, now=now)
            if marker is None:
                # Raced to zero between count and select — fall through.
                return FallThrough()
            return await self._resolve(marker, owner_phone, message_text, None, now)

        # >1 outstanding and no code: cannot disambiguate — remind, resume none.
        return await self._build_reminder(business_id, now)

    async def _resolve(
        self,
        marker: PendingAction,
        owner_phone: str,
        message_text: str,
        code: str | None,
        now: datetime,
    ) -> ResumeResolution:
        """CAS AWAITING_OWNER_REPLY -> RESUME_REQUESTED and persist the answer.

        The answer is the raw reply MINUS the code token (untrusted — the voice P3
        reconciles it through the availability engine). Exactly-once: a racing
        reply that already flipped THIS marker gets rowcount 0 -> stale reminder,
        never a second write.

        Digest integrity (cross-lane): mutating proposed_payload WITHOUT
        recomputing payload_digest leaves the digest stale, and P3's validated read
        (``_validated_stored_payload`` → digest match) would then reject EVERY
        answered marker so the resume never fires. So the payload AND its digest
        move ATOMICALLY in the same conditional_update. Both are derived from the
        SAME validated envelope via the SHARED helpers Dev4 owns
        (``canonical_payload_dict`` for the stored form, and
        ``awaiting_owner_reply_payload_digest`` — the single digest call site both
        lanes use, so P1-create and P2-answer can never drift). Never a duplicate
        digest algorithm.
        """
        answer_text = self._answer_body(message_text, code)
        # Build the resolved payload on top of the marker's existing payload data.
        raw = dict(marker.proposed_payload or {})
        data = dict(raw.get("data") or {})
        data["answer_text"] = answer_text
        data["answered_by_phone"] = owner_phone
        data["answered_at"] = now.isoformat()
        # Leave answer_unused as the marker had it (Dev4 P3 flips it if a late
        # reply can't resume); default False if absent.
        data.setdefault("answer_unused", False)
        raw["data"] = data
        raw.setdefault("schema_version", 1)
        raw.setdefault("action_type", PendingActionType.AWAITING_OWNER_REPLY.value)

        # Canonical stored form from the validated envelope, and the digest from
        # Dev4's shared helper (validates the same dict then digests) — both from
        # the identical validated input, so stored payload and digest agree exactly
        # with what _validated_stored_payload re-derives on read.
        envelope = validate_payload(
            PendingActionType.AWAITING_OWNER_REPLY,
            int(marker.payload_schema_version),
            raw,
        )
        canonical = canonical_payload_dict(envelope)
        digest = awaiting_owner_reply_payload_digest(canonical)

        updated = await self._pending.conditional_update(
            business_id=marker.business_id,
            action_id=marker.id,
            expected_version=marker.version,
            expected_status=PendingActionStatus.AWAITING_OWNER_REPLY,
            values={
                "status": PendingActionStatus.RESUME_REQUESTED.value,
                "proposed_payload": canonical,
                "payload_digest": digest,
                "confirmed_by": owner_phone,
                "confirmed_at": now,
            },
        )
        if updated is None:
            # A concurrent reply already resolved this exact marker.
            return DisambiguationReminder(
                "We've already recorded your reply for that one — thanks."
            )
        return Resumed("Thanks — I've passed your reply back to the call.")

    def _answer_body(self, message_text: str, code: str | None) -> str:
        """The reply text with the code token stripped, bounded to the schema max.

        The answer is untrusted free text; we only strip the correlation token so
        the stored evidence is the owner's actual answer, and cap it at 500.
        """
        text = (message_text or "").strip()
        if code:
            # Remove a leading/bracketed/`code:`-prefixed occurrence of the code.
            text = re.sub(
                rf"(?:\bcode\b\s*[:#]?\s*)?\[?{re.escape(code)}\]?",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
        return text[:_ANSWER_MAX]

    async def _build_reminder(self, business_id: int, now: datetime) -> DisambiguationReminder:
        """Compose the >1-waits reminder: codes + a minimal non-PII distinguisher.

        NEVER patient name/phone/identifiers. Full codes are shown to the owner
        (they own the clinic) but never logged.
        """
        markers = await self._markers.list_awaiting_for_reminder(business_id, now=now)
        lines = []
        for m in markers:
            qt = (m.query_type or "question").replace("_", " ")
            # Time-since-ask as a coarse, non-PII distinguisher.
            mins = self._minutes_since(m.created_at, now)
            lines.append(f"  • {m.correlation_code} — {qt} (asked {mins} min ago)")
        body = "\n".join(lines)
        return DisambiguationReminder(
            "You have more than one pending question. Reply with the exact code "
            "for the one you're answering:\n" + body
        )

    @staticmethod
    def _minutes_since(created_at: datetime | None, now: datetime) -> int:
        if created_at is None:
            return 0
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return max(0, int((now - created_at).total_seconds() // 60))
