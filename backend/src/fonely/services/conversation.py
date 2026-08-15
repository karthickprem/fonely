"""Conversation orchestrator for dental appointment booking."""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from fonely.core.config import settings
from fonely.core.validators import utcnow
from fonely.domain.conversation.safety import (
    ESCALATION_MEDICAL,
    ESCALATION_URGENT,
    SafetyClassification,
    classify_intent,
    detect_confirmation,
)
from fonely.domain.conversation.sanitize import sanitize_llm_response
from fonely.domain.conversation.state import (
    ConversationContext,
    ConversationIntent,
    ConversationState,
    ConversationTurn,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import Channel, PendingActionType
from fonely.services.model_gateway import ModelGateway, ModelResponse

if TYPE_CHECKING:
    from fonely.domain.booking.datetime_parse import TimeSpec
    from fonely.services.conversation_tools import ResourceInfo

logger = logging.getLogger("fonely.services.conversation")

_CONVERSATIONS: dict[str, ConversationContext] = {}
_CONVERSATION_LOCKS: dict[str, asyncio.Lock] = {}
_MAX_CONVERSATIONS = 1000
_CONVERSATION_TTL_SECONDS = 3600

_TIMEOUT_RESPONSE = "I'm having trouble right now. Please try again or call the clinic directly."


def _evict_stale() -> None:
    now = utcnow()
    stale = [
        cid
        for cid, ctx in _CONVERSATIONS.items()
        if (now - ctx.created_at).total_seconds() > _CONVERSATION_TTL_SECONDS
    ]
    for cid in stale:
        del _CONVERSATIONS[cid]
        _CONVERSATION_LOCKS.pop(cid, None)
        for key, indexed_id in list(_PHONE_INDEX.items()):
            if indexed_id == cid:
                _PHONE_INDEX.pop(key, None)
    if stale:
        logger.info("conversations_evicted", extra={"count": len(stale)})

    if len(_CONVERSATIONS) >= _MAX_CONVERSATIONS:
        sorted_convs = sorted(_CONVERSATIONS.items(), key=lambda x: x[1].created_at)
        to_remove = len(_CONVERSATIONS) - _MAX_CONVERSATIONS + 1
        for cid, _ in sorted_convs[:to_remove]:
            del _CONVERSATIONS[cid]
            _CONVERSATION_LOCKS.pop(cid, None)
        logger.info("conversations_evicted_capacity", extra={"count": to_remove})


def invalidate_conversation_cache(business_id: int, phone: str) -> None:
    """Remove cached conversation state for a tenant+phone after rollback."""
    key = (business_id, phone)
    conv_id = _PHONE_INDEX.pop(key, None)
    if conv_id:
        _CONVERSATIONS.pop(conv_id, None)
        _CONVERSATION_LOCKS.pop(conv_id, None)


def _get_lock(conversation_id: str) -> asyncio.Lock:
    lock = _CONVERSATION_LOCKS.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _CONVERSATION_LOCKS[conversation_id] = lock
    return lock


_REQUIRED_FACTS = ("service_id", "resource_id", "start_at", "customer_phone")

# Bare meridiem answers to a "which one — AM or PM?" question, in English,
# Tamil, and Tanglish. These let the patient resolve an ambiguity with the
# word they actually say, so the question is never a dead-end loop.
#
# Two tiers, because "am" is also the most common English verb and "I am not
# sure" must NOT book a morning slot:
# - _*_ANYWHERE words are unambiguous meaning-words; they resolve wherever they
#   appear in the reply ("ok evening please").
# - _*_STANDALONE forms (the two-letter am/pm and dotted a.m/p.m) resolve ONLY
#   when they are effectively the entire answer, so a sentence containing "am"
#   as a verb does not resolve. "pm" has no English homograph but is kept
#   standalone-only for symmetry; "evening"/"morning" cover the sentence case.
# "pagal"/"பகல்" is deliberately omitted: it means daytime broadly and a patient
# can mean late morning by it, so mapping it to a half-of-day would be a guess
# we then book — let the bound catch it instead.
_PM_ANYWHERE = (
    "evening",
    "afternoon",
    "night",
    "maalai",
    "மாலை",
    "iravu",
    "இரவு",
    "mathiyaanam",
    "மதியம்",
)
_AM_ANYWHERE = (
    "morning",
    "kaalai",
    "காலை",
    "forenoon",
)
_PM_STANDALONE = ("pm", "p.m", "p.m.")
_AM_STANDALONE = ("am", "a.m", "a.m.")


def _bare_meridiem_word(message: str) -> str | None:
    """Return 'pm'/'am' if the message names a meridiem/part-of-day, else None.

    Used only to resolve a pending two-slot ambiguity, where the offered set is
    authoritative and the patient just needs to pick a half of the day. The
    two-letter forms ('am'/'pm') resolve only as a standalone answer so that an
    ordinary sentence like "I am not sure" never books a morning slot.
    """
    t = message.strip().lower()

    def _has_anywhere(words: tuple[str, ...]) -> bool:
        return any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", t) for w in words)

    # Standalone = the meridiem token is the whole answer once trivial filler
    # (affirmations, politeness, spoken particles) is stripped. "yes pm",
    # "pm please", "pm ah"/"pm da" (Tanglish), "PM." (STT trailing period) all
    # count; "I am not sure" does not, and crucially the NEGATIONS "no am" /
    # "not pm" do NOT — "no"/"not" are never filler, so they correctly leave
    # two tokens and fail the standalone test.
    raw = re.sub(r"[^\w.]+", " ", t).split()
    # Strip trailing dots from a token unless the token IS a dotted meridiem
    # form (a.m / p.m / a.m. / p.m.), so "pm." -> "pm" but "a.m." is preserved.
    _dotted = {"a.m", "p.m", "a.m.", "p.m."}
    tokens = []
    for tok in raw:
        norm = tok if tok in _dotted else tok.rstrip(".")
        if norm:
            tokens.append(norm)
    _filler = (
        "ok",
        "okay",
        "yes",
        "yeah",
        "yup",
        "please",
        "pls",
        "ah",
        "aa",
        "da",
        "na",
    )
    core = [tok for tok in tokens if tok not in _filler]

    def _is_standalone(forms: tuple[str, ...]) -> bool:
        return len(core) == 1 and core[0] in forms

    pm = _has_anywhere(_PM_ANYWHERE) or _is_standalone(_PM_STANDALONE)
    am = _has_anywhere(_AM_ANYWHERE) or _is_standalone(_AM_STANDALONE)
    if pm and not am:
        return "pm"
    if am and not pm:
        return "am"
    return None


# Honorifics/titles a patient may say (or drop) around a doctor's name, in
# English and Tanglish. Spoken speech omits punctuation and reorders these, so
# "dr priya", "priya doctor", "doctor priya." must all match stored "Dr. Priya".
# Titles are stripped from BOTH the spoken text and the stored name before
# comparison, so matching is on the distinctive name tokens only, and a title
# ADJACENT to a name token is the primary evidence the token was used to NAME a
# doctor (see _match_spoken_resources).
_RESOURCE_TITLES = frozenset(
    {
        "dr",
        "dr.",
        "doctor",
        "dho",  # common Tanglish transliteration of "doctor"
        "mr",
        "mr.",
        "mrs",
        "mrs.",
        "ms",
        "ms.",
        "miss",
        "vaidhyar",  # Tamil: doctor/physician
        "vaidyar",
    }
)

# The subset of titles that UNAMBIGUOUSLY signal a doctor is being named. Used
# by _names_a_resource to decide the unknown-doctor re-ask. "mr"/"ms"/"mrs"/
# "miss" are excluded because they double as ordinary words ("i don't want to
# MISS my slot") and would trip the refusal on sentences that name no doctor.
# They remain in _RESOURCE_TITLES for stripping and title-adjacency, so "ms
# priya" still matches — they just do not, alone, assert a doctor was named.
_NAMING_TITLES = frozenset({"dr", "dr.", "doctor", "dho", "vaidhyar", "vaidyar"})


def _tokenize(text: str) -> list[str]:
    return re.sub(r"[^\w\s]+", " ", text.lower()).split()


def _resource_name_tokens(text: str) -> frozenset[str]:
    """Distinctive lowercase name tokens, with titles and punctuation removed.

    Used to compare a spoken resource name against a stored one independent of
    honorific ("dr"/"doctor"), casing, word order, punctuation, and repeated
    internal whitespace. Returns an EMPTY set if nothing but titles/filler
    remains, so a bare "doctor" never matches a specific resource — that is an
    unnamed resource and must fail closed upstream, not silently pick one.
    """
    return frozenset(tok for tok in _tokenize(text) if tok not in _RESOURCE_TITLES)


def _canonical_callback_candidates(candidates: Iterable[object]) -> list[str]:
    """Canonicalize ambiguity candidates into display strings the callback payload accepts.

    The give-up callback (#36/#41) carries the doctor/slot names we could not
    disambiguate so a human can call back and finish. The raw candidates are the
    ``_resource_ambiguous`` entries — dicts shaped ``{"id": int, "name": str}`` —
    and a candidate may be missing its ``name``, carry ``None``, be whitespace-
    only, or (defensively) not be a ``str`` at all. The original construction was
    ``[str(c.get("name", "")) for c in cand]``, which is a latent bug: a missing
    or blank name becomes ``""`` (violating ``CallbackData`` ``min_length=1`` and
    raising inside PendingAction.create), and ``None`` becomes the literal
    ``"None"``. Because ``_persist_voice_callback`` persists best-effort, that
    ValidationError is swallowed and the DURABLE callback is silently dropped —
    the exact "absence reads as success" trap the callback exists to prevent.

    So extract and canonicalize here, before the payload is built, applying the
    same bounds the schema enforces per element (each name stripped, non-blank,
    truncated to 200 chars) and per list (at most 20, in first-seen order,
    de-duplicated on the canonical form). A non-Mapping candidate, or one whose
    ``name`` is missing / non-string / blank, is DROPPED — never coerced to a
    placeholder, because a fabricated "None"/"unknown" name would mislead the
    human who reads the worklist. An all-invalid input therefore yields ``[]``,
    which the schema allows (the list has no ``min_length``); the caller still
    persists the callback with an empty candidate list rather than losing the
    follow-up.
    """
    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        name = candidate.get("name")
        if not isinstance(name, str):
            continue
        cleaned = name.strip()[:200]
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
        if len(out) >= 20:
            break
    return out


def _names_a_resource(message: str) -> bool:
    """True if the message uses an unambiguous doctor title ("dr", "doctor", …).

    Signals the patient was NAMING a doctor, so a zero-match result is an
    UNKNOWN doctor (fail closed + re-ask) rather than simply no doctor mentioned
    yet (ask normally). Uses _NAMING_TITLES only, so ordinary words that happen
    to be honorifics ("miss", "mr") do not spuriously trip the unknown path.
    """
    return any(tok in _NAMING_TITLES for tok in _tokenize(message))


def _naming_tokens(message: str, all_name_tokens: frozenset[str]) -> set[str]:
    """Message tokens that were plausibly used TO NAME a doctor, not merely to
    appear.

    The core safety distinction (D3 item #19 rejection): a bare name token that
    is also ordinary vocabulary — "mani" is Tanglish for o'clock AND a common
    Tamil name — must NOT count as naming a doctor, or "aaru mani" (6 o'clock)
    silently books "Dr. Mani". A stored-name token in the message counts as
    naming only when it carries positional evidence of being a name:
      - it is adjacent to a title ("dr priya", "priya doctor"), or
      - it is adjacent to another stored-name token ("priya rao", "kumar arun").
    A lone name token surrounded by non-name words ("aaru MANI kaalai") is just
    vocabulary and is ignored. This keeps cross-token ambiguity working while
    closing the time-word / service-word collision hole.
    """
    toks = _tokenize(message)
    n = len(toks)
    named: set[str] = set()
    for i, tok in enumerate(toks):
        if tok not in all_name_tokens:
            continue
        neighbors = []
        if i > 0:
            neighbors.append(toks[i - 1])
        if i < n - 1:
            neighbors.append(toks[i + 1])
        if any(nb in _RESOURCE_TITLES for nb in neighbors) or any(
            nb in all_name_tokens for nb in neighbors
        ):
            named.add(tok)
    return named


def _match_spoken_resources(message: str, resources: "list[ResourceInfo]") -> "list[ResourceInfo]":
    """Return the active resources the message NAMES, top-scored.

    A resource is scored by how many of its distinctive stored-name tokens
    appear in the message AND were used to name someone (see _naming_tokens —
    adjacent to a title or another name token). Scoring on *naming* tokens, not
    mere token appearance, is what prevents a time word ("mani") or service word
    ("general") that collides with a doctor's name from selecting that doctor.

    Top-scorers only:
    - exactly one  -> unambiguous match, the caller sets it;
    - more than one -> a TIE (e.g. "dr priya" with both Priya Kumar and Priya
      Rao) -> the caller MUST fail closed and ask which one; never guess;
    - none          -> no doctor named -> caller asks / refuses; never falls
      through to "any available".
    """
    all_name_tokens: frozenset[str] = (
        frozenset().union(*(_resource_name_tokens(r.name) for r in resources))
        if resources
        else frozenset()
    )
    named = _naming_tokens(message, all_name_tokens)
    if not named:
        return []
    scored: list[tuple[int, ResourceInfo]] = []
    for res in resources:
        stored = _resource_name_tokens(res.name)
        overlap = len(stored & named)
        if overlap > 0:
            scored.append((overlap, res))
    if not scored:
        return []
    top = max(score for score, _ in scored)
    return [res for score, res in scored if score == top]


_ORDINAL_WORDS: dict[str, int] = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "last": -1,
}


def _resolve_disambiguation_reply(
    message: str, candidates: "list[dict[str, object]]"
) -> "int | list[dict[str, object]] | None":
    """Resolve a reply to a "which doctor?" question against the CANDIDATE SET.

    This runs ONLY when a resource ambiguity is already pending, so the match
    surface is the two or three offered candidates — not the full roster. That
    tiny, known surface is what makes relaxed matching safe here where it would
    be unsafe in open speech: a bare surname ("rao"), a bare surname with filler
    ("rao please"), or an ordinal/positional reference ("the second one", "the
    last") is exactly how a person answers this question, and cannot reopen the
    time-word/service-word collision class because it is scored against the
    candidates the patient was just offered.

    Returns:
      - an int resource_id  -> resolved to exactly one candidate;
      - None                -> the reply matched no candidate (caller re-asks);
      - the candidates list  -> the reply still matched more than one (re-ask).
    """
    toks = _tokenize(message)
    tok_set = set(toks)

    # 1. Name-token match against candidates (relaxed: bare token, no adjacency
    # requirement — the pending question is the naming context). Score by how
    # many stored tokens the reply shares, so a distinguishing token wins even
    # when a shared one is also present: "priya rao" scores Rao 2 vs Kumar 1.
    scored = [(len(_resource_name_tokens(str(c.get("name", ""))) & tok_set), c) for c in candidates]
    scored = [(n, c) for n, c in scored if n > 0]
    name_hits_multi: list[dict[str, object]] = []
    if scored:
        top = max(n for n, _ in scored)
        top_hits = [c for n, c in scored if n == top]
        if len(top_hits) == 1:
            return int(str(top_hits[0]["id"]))
        # A tie at the top (e.g. only the shared "priya" was said) narrows
        # nothing; fall through to ordinal, else report still-ambiguous.
        name_hits_multi = top_hits

    # 2. Ordinal / positional reference against the OFFERED ORDER, including a
    # bare digit ("1"/"2") for the numbered-choice escalation.
    for tok in toks:
        pos: int | None = None
        if tok in _ORDINAL_WORDS:
            pos = _ORDINAL_WORDS[tok]
        elif tok.isdigit():
            pos = int(tok)
        if pos is not None:
            idx = len(candidates) - 1 if pos == -1 else pos - 1
            if 0 <= idx < len(candidates):
                return int(str(candidates[idx]["id"]))

    if name_hits_multi:
        return name_hits_multi
    return None


# Channel-specific terminal wording for a give-up path (CEO #33). The ladder
# logic that decides WHEN to give up is channel-agnostic; only the final string
# varies, so this stays a bounded policy table rather than `if channel == voice`
# branches scattered through the flow. Each string must be TRUE for its channel
# given the capabilities that actually exist:
#   TEXT  -> the patient can be told to call the clinic (they are on WhatsApp).
#   VOICE -> they are ALREADY connected to the clinic's number, so "call the
#            clinic" is incoherent. We have NO transfer and NO durable callback
#            capability today (durable callback is CEO #36, post-freeze), so the
#            voice wording promises NOTHING it cannot perform: it apologizes and
#            states plainly the booking did not complete. Saying "transferring
#            you" or "we'll call back" would be a FALSE PROMISE that leaves a
#            caller waiting on a dead line — worse than incoherent.
_DISAMBIGUATION_GIVEUP_TEXT: dict[Channel, str] = {
    Channel.TEXT: (
        "I'm sorry, I couldn't tell which doctor you meant. "
        "Please call the clinic directly and they'll help you book."
    ),
    Channel.VOICE: (
        "I'm sorry, I couldn't tell which doctor you meant, so I wasn't able to "
        "book the appointment. No appointment has been made."
    ),
}


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        model: ModelGateway,
        *,
        appointment_service: object,
    ) -> None:
        self._session = session
        self._model = model
        self._appointment_service = appointment_service

    async def process_message(
        self,
        conversation_id: str,
        business_id: int,
        actor: ActorContext,
        user_message: str,
    ) -> ConversationTurn:
        from fonely.core.pii_audit import log_pii_access

        log_pii_access(
            operation="read",
            data_type="conversation",
            business_id=business_id,
            accessor="service:conversation",
            record_count=1,
        )
        lock = _get_lock(conversation_id)
        async with lock:
            try:
                async with asyncio.timeout(settings.conversation_timeout_seconds):
                    turn = await self._process_inner(
                        conversation_id, business_id, actor, user_message
                    )
                    await self._persist_turn(conversation_id, turn)
                    return turn
            except TimeoutError:
                logger.warning(
                    "conversation_timeout",
                    extra={"conversation_id": conversation_id},
                )
                ctx = _CONVERSATIONS.get(conversation_id)
                if ctx is None:
                    ctx = ConversationContext(
                        conversation_id=conversation_id,
                        business_id=business_id,
                    )
                return self._make_turn(
                    ctx,
                    user_message,
                    _TIMEOUT_RESPONSE,
                    ConversationIntent.UNKNOWN,
                    "administrative",
                )

    async def _process_inner(
        self,
        conversation_id: str,
        business_id: int,
        actor: ActorContext,
        user_message: str,
    ) -> ConversationTurn:
        start_time = time.monotonic()

        ctx = _CONVERSATIONS.get(conversation_id)
        if ctx is None:
            try:
                from fonely.services.conversation_persistence import (
                    ConversationPersistenceService,
                )

                persistence = ConversationPersistenceService(self._session)
                loaded = await persistence.load_by_id(conversation_id)
                if loaded is not None:
                    ctx = loaded
                    _CONVERSATIONS[conversation_id] = ctx
            except Exception:
                logger.debug("db_conversation_load_skipped", exc_info=True)
        if ctx is None:
            ctx = ConversationContext(
                conversation_id=conversation_id,
                business_id=business_id,
            )
            _CONVERSATIONS[conversation_id] = ctx

        if ctx.at_turn_limit:
            turn = self._end_turn(
                ctx,
                user_message,
                "We've reached the conversation limit. "
                "Please call the clinic directly for further assistance.",
                ConversationIntent.UNKNOWN,
                "administrative",
            )
            self._log_turn(turn, start_time)
            return turn

        safety = classify_intent(user_message)

        if safety.classification == "urgent_medical":
            turn = self._escalate_turn(ctx, user_message, safety, ESCALATION_URGENT)
            self._log_turn(turn, start_time)
            return turn

        if safety.classification == "medical":
            turn = self._escalate_turn(ctx, user_message, safety, ESCALATION_MEDICAL)
            self._log_turn(turn, start_time)
            return turn

        if ctx.state == ConversationState.AWAITING_CONFIRMATION:
            turn = await self._handle_confirmation(ctx, user_message, actor, safety)
            self._log_turn(turn, start_time)
            return turn

        if ctx.state == ConversationState.CANCEL_SELECTION:
            turn = await self._handle_cancel_selection(ctx, user_message, actor, safety)
            self._log_turn(turn, start_time)
            return turn

        if ctx.state == ConversationState.RESCHEDULE_SELECTION:
            turn = await self._handle_reschedule_selection(ctx, user_message, actor, safety)
            self._log_turn(turn, start_time)
            return turn

        if ctx.state == ConversationState.GREETING:
            ctx.transition(ConversationState.INTENT_RECOGNITION)

        if ctx.state == ConversationState.INTENT_RECOGNITION:
            if safety.intent == ConversationIntent.CANCEL_APPOINTMENT:
                turn = await self._handle_cancel_intent(ctx, user_message, actor, safety)
                self._log_turn(turn, start_time)
                return turn
            if safety.intent == ConversationIntent.RESCHEDULE:
                turn = await self._handle_reschedule_intent(ctx, user_message, actor, safety)
                self._log_turn(turn, start_time)
                return turn
            ctx.transition(ConversationState.FACT_COLLECTION)
            ctx.collected_facts["_operation"] = "book"

        from fonely.services.conversation_tools import get_business_context

        biz = await get_business_context(business_id, self._session)
        if biz is None:
            turn = self._end_turn(
                ctx,
                user_message,
                "Clinic not found.",
                ConversationIntent.UNKNOWN,
                "administrative",
            )
            self._log_turn(turn, start_time)
            return turn

        await self._extract_facts(ctx, user_message, biz)
        await self._validate_facts(ctx, biz)

        # A bare time that matched two offered slots is ambiguous: keep the
        # offer and ask which one, rather than dropping known context. Bounded
        # so the question can never repeat forever no matter what arrives — a
        # patient answer that resolves it clears the flag in _try_offer_selection
        # (an English ordinal, a full time, or a bare meridiem word in either
        # language); anything else counts toward the bound and then we fall back.
        ambiguous = ctx.collected_facts.get("_selection_ambiguous")
        if ambiguous and isinstance(ambiguous, list):
            asked_raw = ctx.collected_facts.get("_ambiguity_asks", 0)
            asked = asked_raw if isinstance(asked_raw, int) else 0
            if asked >= 2:
                # Escape hatch: stop looping. Drop the stale offer/ambiguity and
                # ask for the time plainly so the turn cannot recur.
                ctx.collected_facts.pop("_selection_ambiguous", None)
                ctx.collected_facts.pop("_ambiguity_asks", None)
                self._drop_active_offer(ctx)
                ctx.collected_facts.pop("start_at", None)
                turn = self._fact_turn(
                    ctx,
                    user_message,
                    "Sorry, I didn't catch which time. What time would you "
                    "like — for example '10:30 AM' or '6 PM'?",
                    safety,
                    ["start_at"],
                )
                self._log_turn(turn, start_time)
                return turn
            ctx.collected_facts["_ambiguity_asks"] = asked + 1
            options = " or ".join(
                str(x.get("display", x)) if isinstance(x, dict) else str(x) for x in ambiguous
            )
            turn = self._fact_turn(
                ctx,
                user_message,
                f"Did you mean {options}? Which one works for you?",
                safety,
                ["start_at"],
            )
            self._log_turn(turn, start_time)
            return turn

        # A spoken resource name that matched more than one active doctor is
        # ambiguous: fail closed and ask which one, never pick. Booking the wrong
        # doctor is a silent mis-booking. A later turn naming one doctor
        # unambiguously clears the flag in _extract_facts (single match).
        resource_ambiguous = ctx.collected_facts.get("_resource_ambiguous")
        if resource_ambiguous and isinstance(resource_ambiguous, list):
            asked_raw = ctx.collected_facts.get("_resource_ambiguity_asks", 0)
            asked = asked_raw if isinstance(asked_raw, int) else 0
            cand = [r for r in resource_ambiguous if isinstance(r, dict)]
            # Bound the loop so it TERMINATES — not merely swaps one repeating
            # question for another (CEO #32). Three escalating stages, then the
            # state is genuinely left so no question can recur:
            #   ask 0-1: plain "which doctor?"
            #   ask 2:   numbered choice (a crisp path the relaxed answer path
            #            resolves via "1"/"2")
            #   ask >=3: give up disambiguating — DROP the ambiguity flags (so
            #            this branch cannot re-enter) and route to a terminating
            #            escape, exactly like the time-selection bound above.
            if asked >= 3:
                ctx.collected_facts.pop("_resource_ambiguous", None)
                ctx.collected_facts.pop("_resource_ambiguity_asks", None)
                self._drop_active_offer(ctx)
                # On VOICE the caller is on a live call with no channel to return
                # to — a give-up that persists nothing leaves them with no
                # follow-up (#36). Leave a durable callback carrying the partial
                # facts so a human can call back and finish the booking. Text
                # callers keep a WhatsApp thread they can resume, so no callback
                # there. Best-effort: never fail the give-up on a callback error.
                if actor.channel == Channel.VOICE:
                    await self._persist_voice_callback(
                        ctx,
                        actor,
                        reason_code="doctor_disambiguation_exhausted",
                        attempted_candidates=_canonical_callback_candidates(cand),
                    )
                # Terminal wording is channel-specific (CEO #33): on voice the
                # caller is already connected, so "call the clinic" is a false
                # instruction. Keyed off the AUTHORITATIVE actor.channel, never
                # anything the caller said.
                turn = self._end_turn(
                    ctx,
                    user_message,
                    _DISAMBIGUATION_GIVEUP_TEXT[actor.channel],
                    ConversationIntent.UNKNOWN,
                    "administrative",
                )
                self._log_turn(turn, start_time)
                return turn
            ctx.collected_facts["_resource_ambiguity_asks"] = asked + 1
            if asked >= 2:
                numbered = "; ".join(f"{i + 1}. {c.get('name', '')}" for i, c in enumerate(cand))
                amb_prompt = f"Please reply with the number of the doctor you want — {numbered}."
            else:
                names = " or ".join(str(c.get("name", "")) for c in cand)
                amb_prompt = f"We have more than one: {names}. Which doctor would you like?"
            turn = self._fact_turn(ctx, user_message, amb_prompt, safety, ["resource_id"])
            self._log_turn(turn, start_time)
            return turn

        # A named-but-unknown doctor also fails closed: re-ask with the roster
        # rather than proceeding to any-available. Cleared once a known name is
        # matched (see _extract_facts single-match branch).
        if ctx.collected_facts.get("_resource_unknown"):
            roster = ", ".join(r.name for r in biz.resources)
            turn = self._fact_turn(
                ctx,
                user_message,
                f"I couldn't find that doctor. We have: {roster}. Which doctor would you like?",
                safety,
                ["resource_id"],
            )
            self._log_turn(turn, start_time)
            return turn

        missing = self._identify_missing_facts(ctx)

        if not missing and ctx.state == ConversationState.FACT_COLLECTION:
            turn = await self._check_availability_and_propose(ctx, user_message, actor, biz, safety)
            self._log_turn(turn, start_time)
            return turn

        response = await self._generate_response(ctx, user_message, biz, missing, safety)
        turn = self._fact_turn(
            ctx,
            user_message,
            sanitize_llm_response(response.text),
            safety,
            missing,
        )
        self._log_turn(turn, start_time)
        return turn

    @staticmethod
    def _drop_active_offer(ctx: ConversationContext) -> None:
        """Discard the active offer AND its selection pointers together.

        _active_offer, _selected_token and _selected_offer_id are logically
        inseparable: a selection pointer without the offer it indexes is ALWAYS
        invalid — the token/offer_id name a slot in an offer that no longer
        exists. Every abandon/invalidation site must clear all three, or the
        pointers go stale and leak into the persisted conversation row (they are
        written at _try_offer_selection and never read again, so nothing catches
        the staleness at runtime — but a persisted snapshot would show a
        selection at a slot whose offer is gone, and any future consumer that
        trusts them, e.g. resume-selection or audit/repair, would read a lie).

        This clears ONLY those three. It deliberately does NOT touch the other
        co-state (_selection_ambiguous, _resource_ambiguous, start_at, …): the
        call sites clear DIFFERENT subsets of that state on purpose, and a
        blanket clear here would over-clear state some sites intentionally keep.
        Each site keeps its own additional pops; this only replaces the bare
        pop("_active_offer") and adds the two pointer clears.
        """
        ctx.collected_facts.pop("_active_offer", None)
        ConversationService._clear_selection_pointers(ctx)

    @staticmethod
    def _clear_selection_pointers(ctx: ConversationContext) -> None:
        """Clear ONLY the selection pointers, KEEPING the active offer.

        Distinct from _drop_active_offer: this is for the one abandon event where
        the offer legitimately SURVIVES but the current SELECTION is rejected — a
        bare "no" to a proposed slot at AWAITING_CONFIRMATION. The caller may
        still pick a DIFFERENT slot from the same offer (verified reselection
        flow: "no" keeps _active_offer, then "the first one" re-selects and
        re-proposes), so dropping the offer would break reject-then-reselect.

        But the rejected slot's pointers must not linger: if the caller does NOT
        reselect (asks something else, goes quiet), the old _selected_token /
        _selected_offer_id point at the slot they just refused and leak into the
        persisted conversation row. The reselect path is self-healing (it
        overwrites the pointers), so this only matters for the no-reselect case —
        which is exactly the case that strands stale state.
        """
        ctx.collected_facts.pop("_selected_token", None)
        ctx.collected_facts.pop("_selected_offer_id", None)

    @staticmethod
    def _invalidate_offer_if_changed(
        ctx: ConversationContext, fact_key: str, new_value: object
    ) -> None:
        old = ctx.collected_facts.get(fact_key)
        if old is not None and old != new_value:
            ConversationService._drop_active_offer(ctx)

    async def _extract_facts(self, ctx: ConversationContext, message: str, biz: object) -> None:
        from fonely.services.conversation_tools import BusinessContext

        assert isinstance(biz, BusinessContext)
        msg_lower = message.lower()

        regex_found = False

        for svc in biz.services:
            if svc.name.lower() in msg_lower:
                if ctx.collected_facts.get("service_id") != svc.id:
                    self._invalidate_offer_if_changed(ctx, "service_id", svc.id)
                    ctx.collected_facts["service_id"] = svc.id
                    ctx.collected_facts["service_name"] = svc.name
                    regex_found = True
                break

        # If a doctor ambiguity is already pending, THIS turn is the patient's
        # answer to our own "which doctor?" question. Resolve it against the
        # CANDIDATE SET with relaxed matching (bare surname, filler, ordinal) —
        # safe because the surface is the 2-3 offered candidates, not the whole
        # roster, so it cannot reopen the time/service-word collision class. Open-
        # speech matching below stays strict. (Rescope-2 item 2/3.)
        disambiguation_pending = False
        pending_candidates = ctx.collected_facts.get("_resource_ambiguous")
        if isinstance(pending_candidates, list) and pending_candidates:
            disambiguation_pending = True
            disambig = _resolve_disambiguation_reply(message, pending_candidates)
            if isinstance(disambig, int):
                chosen = next((r for r in biz.resources if r.id == disambig), None)
                if chosen is not None:
                    self._invalidate_offer_if_changed(ctx, "resource_id", chosen.id)
                    ctx.collected_facts["resource_id"] = chosen.id
                    ctx.collected_facts["resource_name"] = chosen.name
                    ctx.collected_facts.pop("_resource_ambiguous", None)
                    ctx.collected_facts.pop("_resource_ambiguity_asks", None)
                    regex_found = True
            # Unresolved (matched none, or still >1): leave the flag set; the
            # _process_inner bound escalates after repeated asks.

        # Spoken resource-name matching. A patient says "dr priya" / "priya
        # doctor" / "doctor priya." for stored "Dr. Priya"; naive substring
        # matching missed all of these. _match_spoken_resources normalizes both
        # sides (title/case/order/punctuation) and returns EVERY match so we can
        # fail closed on ambiguity instead of silently booking the wrong doctor
        # (a class-1 silent mis-booking, the exact defect this family prevents).
        # Skipped entirely while a disambiguation is pending: this turn is an
        # ANSWER to our "which doctor?" question (handled above against the
        # candidate set), not fresh open-speech naming. Running it here would
        # re-flag the shared first name ("priya") or spuriously set
        # _resource_unknown on a non-matching answer.
        matched = [] if disambiguation_pending else _match_spoken_resources(message, biz.resources)
        if "service_id" in ctx.collected_facts:
            # Once the service is known, only eligible resources are candidates —
            # an ineligible same-named resource must not create false ambiguity.
            sid = ctx.collected_facts["service_id"]
            eligible_ids = {rid for s, rid in biz.eligibility if s == sid}
            matched = [r for r in matched if r.id in eligible_ids]
        if len(matched) > 1:
            # Fail closed: refuse to guess. Record the candidates so the next
            # turn asks which doctor, and clear any provisionally-set resource so
            # we never proceed on an ambiguous name.
            ctx.collected_facts["_resource_ambiguous"] = [
                {"id": r.id, "name": r.name} for r in matched
            ]
            ctx.collected_facts.pop("_resource_unknown", None)
            ctx.collected_facts.pop("resource_id", None)
            ctx.collected_facts.pop("resource_name", None)
            self._drop_active_offer(ctx)
        elif len(matched) == 1:
            res = matched[0]
            ctx.collected_facts.pop("_resource_ambiguous", None)
            ctx.collected_facts.pop("_resource_unknown", None)
            if ctx.collected_facts.get("resource_id") != res.id:
                self._invalidate_offer_if_changed(ctx, "resource_id", res.id)
                ctx.collected_facts["resource_id"] = res.id
                ctx.collected_facts["resource_name"] = res.name
                regex_found = True
        elif _names_a_resource(message) and "resource_id" not in ctx.collected_facts:
            # The patient referred to a doctor ("dr smith") but it matches no
            # active resource. Fail closed with an explicit re-ask instead of
            # silently proceeding to "any available" — an unknown doctor is a
            # request we cannot honour, not a blank to fill with a default.
            ctx.collected_facts["_resource_unknown"] = True
            self._drop_active_offer(ctx)

        if "customer_phone" not in ctx.collected_facts:
            phone_match = re.search(r"\+?\d{10,13}", message)
            if phone_match:
                phone = phone_match.group()
                if not phone.startswith("+"):
                    phone = "+91" + phone
                if len(phone) >= 12 and not all(c == "0" for c in phone.lstrip("+")):
                    ctx.collected_facts["customer_phone"] = phone
                    regex_found = True

        if "customer_name" not in ctx.collected_facts:
            name_match = re.search(
                r"(?:my name is|i'?m|name:?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
                message,
                re.IGNORECASE,
            )
            if name_match:
                ctx.collected_facts["customer_name"] = name_match.group(1)
                regex_found = True

        # Always attempt datetime extraction so time corrections and offered-slot
        # selections are honored even after start_at is first set.
        had_start = "start_at" in ctx.collected_facts
        self._extract_datetime(ctx, message, biz.timezone)
        if not had_start and "start_at" in ctx.collected_facts:
            regex_found = True

        if not regex_found:
            try:
                from fonely.services.fact_extractor import FactExtractor
                from fonely.services.fact_resolver import FactResolver

                extractor = FactExtractor(self._model)
                extracted = await extractor.extract(message, biz, ctx.collected_facts)
                resolved = FactResolver().resolve(extracted, biz, biz.timezone)
                # If the deterministic matcher already found the spoken name
                # ambiguous, the LLM must NOT resolve it to one resource — that
                # would launder a guess past the fail-closed gate.
                blocked = (
                    {"resource_id", "resource_name"}
                    if ctx.collected_facts.get("_resource_ambiguous")
                    or ctx.collected_facts.get("_resource_unknown")
                    else set()
                )
                for key, value in resolved.to_dict().items():
                    if key not in ctx.collected_facts and key not in blocked:
                        ctx.collected_facts[key] = value
            except Exception:
                logger.warning("llm_fact_extraction_failed", exc_info=True)

    async def _validate_facts(self, ctx: ConversationContext, biz: object) -> None:
        from fonely.services.conversation_tools import BusinessContext

        assert isinstance(biz, BusinessContext)

        if "start_at" in ctx.collected_facts:
            from datetime import datetime

            start = ctx.collected_facts["start_at"]
            assert isinstance(start, datetime)
            if start <= utcnow():
                del ctx.collected_facts["start_at"]
                return

        if "service_id" in ctx.collected_facts and "resource_id" in ctx.collected_facts:
            sid = ctx.collected_facts["service_id"]
            rid = ctx.collected_facts["resource_id"]
            if not any(s == sid and r == rid for s, r in biz.eligibility):
                del ctx.collected_facts["resource_id"]
                ctx.collected_facts.pop("resource_name", None)

    def _try_offer_selection(self, ctx: ConversationContext, message: str) -> bool:
        offer_data = ctx.collected_facts.get("_active_offer")
        if not offer_data or not isinstance(offer_data, dict):
            return False

        from fonely.domain.booking.offers import (
            OfferValidationError,
            deserialize_offer,
            validate_selection,
        )

        try:
            offer = deserialize_offer(offer_data)
        except OfferValidationError:
            self._drop_active_offer(ctx)
            return False
        if offer is None:
            self._drop_active_offer(ctx)
            return False

        from fonely.domain.booking.datetime_parse import parse_time_spec

        matched_slot = None

        # 1. Parse the patient's time and match it against the offered slots.
        # Slot display_time always carries an explicit am/pm. The patient's time
        # often does not ("5:30" for a 5:30 PM slot). When the patient's meridiem
        # is explicit we require an exact (hour, minute) match; when it is a bare
        # hour we match modulo 12 (minute equal, hour ≡ said.hour mod 12) against
        # the authoritative, finite offer set. This is safe disambiguation — not
        # a clinic-hours guess — and we only accept it when EXACTLY ONE offered
        # slot matches; two candidates are genuinely ambiguous, so we ask.
        said = parse_time_spec(message)
        if said is not None:
            candidates = []
            for slot in offer.slots:
                slot_spec = parse_time_spec(slot.display_time)
                if slot_spec is None:
                    continue
                ss = slot_spec.time
                if said.meridiem_explicit:
                    if (ss.hour, ss.minute) == (said.time.hour, said.time.minute):
                        candidates.append(slot)
                elif ss.minute == said.time.minute and (ss.hour % 12) == (said.time.hour % 12):
                    candidates.append(slot)
            if len(candidates) == 1:
                matched_slot = candidates[0]
            elif len(candidates) >= 2:
                # Ambiguous bare time (e.g. "5:30" with both 5:30 AM and 5:30 PM
                # offered). Do NOT drop the offer and do NOT ask for a date we
                # already have — keep the offer and ask WHICH ONE. Returning
                # True consumes the turn without setting start_at, so the offer
                # survives and the missing-fact question becomes "which one".
                # Store the candidates' tokens (with display) so a later bare
                # meridiem answer resolves against exactly THESE two slots, not
                # some other slot in the offer that merely shares a meridiem.
                ctx.collected_facts["_selection_ambiguous"] = [
                    {"display": s.display_time, "token": s.token} for s in candidates
                ]
                return True

        # 1.5 Resolve a pending ambiguity by a bare meridiem word. When we asked
        # "5:30 AM or 5:30 PM?", the natural answer is just "pm" / "evening" /
        # "மாலை" / "காலை" — not a full time or an English ordinal. Resolve ONLY
        # among the two ambiguous candidates (by their stored tokens), never the
        # whole offer, so we cannot book a third slot that merely shares the
        # named meridiem. Escapable in both languages so it never loops.
        pending_amb = ctx.collected_facts.get("_selection_ambiguous")
        if matched_slot is None and isinstance(pending_amb, list):
            half = _bare_meridiem_word(message)
            if half is not None:
                want = "pm" if half == "pm" else "am"
                for entry in pending_amb:
                    if not isinstance(entry, dict):
                        continue
                    display = str(entry.get("display", "")).lower()
                    if want in display:
                        tok = str(entry.get("token", ""))
                        matched_slot = offer.find_by_token(tok)
                        break

        # 2. Word-boundary ordinal matching (only if no time match)
        if matched_slot is None:
            msg_lower = message.strip().lower()
            _ordinals = [
                (r"\bfirst\b", 0),
                (r"\bsecond\b", 1),
                (r"\bthird\b", 2),
            ]
            for pattern, idx in _ordinals:
                if re.search(pattern, msg_lower) and idx < len(offer.slots):
                    matched_slot = offer.slots[idx]
                    break

        if matched_slot is None:
            return False

        # B3 fix: use trusted context ids, not the stored offer's own ids
        try:
            selected = validate_selection(
                offer,
                matched_slot.token,
                business_id=ctx.business_id,
                conversation_id=ctx.conversation_id,
            )
        except OfferValidationError:
            self._drop_active_offer(ctx)
            return False

        ctx.collected_facts["start_at"] = selected.start_at_utc
        ctx.collected_facts["_selected_token"] = selected.token
        ctx.collected_facts["_selected_offer_id"] = selected.offer_id
        ctx.collected_facts.pop("_selection_ambiguous", None)
        ctx.collected_facts.pop("_ambiguity_asks", None)
        return True

    @staticmethod
    def _time_is_directly_negated(message: str) -> bool:
        """True if the message rejects the time it names ("not 5 pm", "no 5").

        A negation word immediately before the time token. This is distinct
        from a correction that names a REPLACEMENT ("no no, make it 6 pm"),
        which _correction_replacement_time handles.
        """
        t = message.lower()
        # negation word, optional filler, then a time token (digit or word-hour
        # or Tamil hour), within a short window.
        neg = (
            r"\b(?:not|no|dont|don't|do not|vendaam|வேண்டாம்|illai|இல்லை)\b"
            r"(?:\s+\w+){0,2}?\s+"
            r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|mani|onnu|rendu|moonu|naalu|anju|aaru|ezhu|ettu|"
            r"onbadhu|pathu)"
        )
        return re.search(neg, t) is not None

    @staticmethod
    def _correction_replacement_spec(message: str) -> "TimeSpec | None":
        """Return the REPLACEMENT TimeSpec in a correction, or None.

        "no no make it 6 pm" / "not 5, change to 6 pm" name a new time after a
        correction cue or after the negated one. We parse only the text that
        introduces the new time, so the negated time is never read as the
        replacement, and we return None when the ONLY time present is the
        negated one ("not 5 pm").
        """
        from fonely.domain.booking.datetime_parse import parse_time_spec

        t = message.lower()
        # 1. Explicit correction cue -> parse the tail after it.
        cue = re.search(
            r"\b(?:make it|change (?:it )?to|instead|rather|"
            r"maathunga|மாத்துங்க)\b(.*)$",
            t,
        )
        if cue is not None:
            return parse_time_spec(cue.group(1))

        # 2. No cue. The negation rejects the time ADJACENT to it. A replacement
        # exists only if a SECOND, later time is named. Doubled negation
        # ("no no 6 pm") has no adjacent time to reject, so its single time is
        # the replacement.
        _time_tok = (
            r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m|p\.m)?)"
            r"|(?:aaru|pathu|anju|ettu|ezhu|moonu|naalu|rendu|onnu|onbadhu|"
            r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
            r"(?:\s*mani)?"
        )
        # A single negation immediately followed by a time = that time rejected.
        adj = re.search(
            rf"\b(?:not|dont|don't|do not|vendaam|வேண்டாம்)\b\s+({_time_tok})",
            t,
        )
        # Doubled/lone "no" negation ("no no 6 pm", "no 6 pm" as rejection of a
        # PRIOR proposal): the time is the replacement, not a rejection.
        if adj is not None:
            # Parse only what follows the rejected token — a second time, if any.
            tail = t[adj.end() :]
            return parse_time_spec(tail)
        # "no ..." / "no no ..." with the time not adjacent to "not": the whole
        # remainder after the negation run is the replacement.
        neg_run = re.search(r"\b(?:no)(?:\s+no)*\b(.*)$", t)
        if neg_run is not None:
            return parse_time_spec(neg_run.group(1))
        return None

    def _extract_datetime(
        self, ctx: ConversationContext, message: str, timezone: str = "Asia/Kolkata"
    ) -> None:
        from datetime import UTC, datetime
        from datetime import time as dt_time
        from zoneinfo import ZoneInfo

        if self._try_offer_selection(ctx, message):
            return

        from datetime import date as _date

        from fonely.domain.booking.datetime_parse import (
            parse_relative_date,
            parse_time_spec,
        )

        clinic_tz = ZoneInfo(timezone)
        now = datetime.now(clinic_tz)

        # Negation handling. "not 5 pm" / "no 5" must NOT be taken as the
        # requested time — booking the negated time is a silent mis-booking.
        # But "no no make it 6 pm" is a CORRECTION: the negation rejects the
        # prior reading while naming a new time. We distinguish by adjacency —
        # a time immediately after a negation word is the rejected one; a time
        # introduced by "make it"/"change to"/"instead" (or simply not adjacent
        # to the negation) is the replacement.
        negated_time = self._time_is_directly_negated(message)

        said_spec = parse_time_spec(message)
        said_time = said_spec.time if said_spec is not None else None
        said_date = parse_relative_date(message, now.date())

        if negated_time:
            # The named time is explicitly rejected. If the message ALSO names a
            # replacement (a correction like "not 5 pm, make it 6"), keep only
            # the replacement; otherwise drop the time and clear any stale
            # start_at so the patient is asked for a new time rather than booked
            # into the one they just refused.
            replacement_spec = self._correction_replacement_spec(message)
            said_spec = replacement_spec
            said_time = replacement_spec.time if replacement_spec is not None else None
            if said_time is None:
                ctx.collected_facts.pop("start_at", None)
                self._drop_active_offer(ctx)
                ctx.collected_facts.pop("_pending_time", None)
                ctx.collected_facts.pop("_pending_time_explicit", None)

        # Merge with any date/time already held from a prior turn. The parser
        # never invents the missing half; pending state carries it forward.
        pending_date_raw = ctx.collected_facts.get("_pending_date")
        pending_time_raw = ctx.collected_facts.get("_pending_time")
        held_date = (
            _date.fromisoformat(pending_date_raw) if isinstance(pending_date_raw, str) else None
        )
        held_time = (
            dt_time.fromisoformat(pending_time_raw) if isinstance(pending_time_raw, str) else None
        )
        # The held time's meridiem-explicitness must survive across turns, or a
        # bare "6 mani" said before the date loses its alternate reading when
        # the date arrives later (defect 1). Default True so an unknown-origin
        # held time is treated as explicit (no spurious alt reading).
        held_time_explicit = bool(ctx.collected_facts.get("_pending_time_explicit", True))

        eff_date = said_date or held_date
        eff_time = said_time or held_time
        # Explicitness of the effective time: this turn's spec if it supplied
        # the time, else the carried-forward flag from the held time.
        if said_time is not None and said_spec is not None:
            eff_time_explicit = said_spec.meridiem_explicit
        else:
            eff_time_explicit = held_time_explicit

        if said_time is None and said_date is None:
            return

        # A newly named time/date makes any active offer stale.
        if said_time is not None or said_date is not None:
            self._drop_active_offer(ctx)
            ctx.collected_facts.pop("_selection_ambiguous", None)
            ctx.collected_facts.pop("_ambiguity_asks", None)

        # Any previously-computed alt reading is stale once we re-extract.
        ctx.collected_facts.pop("_start_at_alt", None)

        if eff_date is not None and eff_time is not None:
            local_dt = datetime.combine(eff_date, eff_time, tzinfo=clinic_tz)
            ctx.collected_facts["start_at"] = local_dt.astimezone(UTC)
            ctx.collected_facts.pop("_pending_date", None)
            ctx.collected_facts.pop("_pending_time", None)
            ctx.collected_facts.pop("_pending_time_explicit", None)
            # When the effective time carried no explicit am/pm — whether it was
            # given this turn OR carried across turns via _pending_time — record
            # the OTHER meridiem reading (hour +/- 12) so availability considers
            # both. This survives the split-turn path (defect 1).
            if not eff_time_explicit:
                alt_hour = (eff_time.hour + 12) % 24
                alt_local = datetime.combine(
                    eff_date, eff_time.replace(hour=alt_hour), tzinfo=clinic_tz
                )
                ctx.collected_facts["_start_at_alt"] = alt_local.astimezone(UTC).isoformat()
        else:
            # Only one half known — never guess the other. Hold it and drop any
            # stale composed start_at so the caller asks for what is missing.
            ctx.collected_facts.pop("start_at", None)
            if eff_date is not None:
                ctx.collected_facts["_pending_date"] = eff_date.isoformat()
            if eff_time is not None:
                ctx.collected_facts["_pending_time"] = eff_time.isoformat()
                ctx.collected_facts["_pending_time_explicit"] = eff_time_explicit

    def _refine_datetime_gap(self, ctx: ConversationContext, missing: list[str]) -> list[str]:
        # When start_at is missing but one half was understood, name the half
        # still needed so the question is precise and we never re-ask for what
        # the patient already gave. A held time -> ask the date; a held date ->
        # ask the time.
        if "start_at" not in missing:
            return missing
        has_time = "_pending_time" in ctx.collected_facts
        has_date = "_pending_date" in ctx.collected_facts
        if has_time and not has_date:
            return ["appointment date" if f == "start_at" else f for f in missing]
        if has_date and not has_time:
            return ["appointment time" if f == "start_at" else f for f in missing]
        return missing

    def _identify_missing_facts(self, ctx: ConversationContext) -> list[str]:
        operation = ctx.collected_facts.get("_operation", "book")
        if operation == "cancel":
            return []
        if operation == "reschedule":
            base = [f for f in ("start_at",) if f not in ctx.collected_facts]
            return self._refine_datetime_gap(ctx, base)
        base = [f for f in _REQUIRED_FACTS if f not in ctx.collected_facts]
        return self._refine_datetime_gap(ctx, base)

    async def _handle_cancel_intent(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import (
            format_appointment_list,
            get_business_context,
            get_patient_appointments,
        )

        ctx.transition(ConversationState.CANCEL_SELECTION)
        ctx.collected_facts["_operation"] = "cancel"

        biz = await get_business_context(actor.business_id, self._session)
        timezone = biz.timezone if biz else "Asia/Kolkata"
        ctx.collected_facts["_business_timezone"] = timezone

        appointments = await get_patient_appointments(
            actor.business_id, actor.normalized_phone, self._session
        )

        if not appointments:
            ctx.transition(ConversationState.ENDED)
            return self._fact_turn(
                ctx, user_message, "You don't have any upcoming appointments.", safety, []
            )

        if len(appointments) == 1:
            appt = appointments[0]
            return await self._create_cancel_proposal(
                ctx, user_message, actor, safety, appt, timezone
            )

        ctx.collected_facts["_candidates"] = [
            {
                "appointment_id": a.appointment_id,
                "service_name": a.service_name,
                "resource_name": a.resource_name,
                "start_at": a.start_at.isoformat(),
                "version": a.version,
                "pending_action_id": a.pending_action_id,
                "service_id": a.service_id,
                "resource_id": a.resource_id,
                "price": a.price,
                "status": a.status,
            }
            for a in appointments
        ]
        listing = format_appointment_list(appointments, timezone)
        return self._fact_turn(
            ctx,
            user_message,
            f"Which appointment would you like to cancel?\n{listing}",
            safety,
            [],
        )

    async def _handle_cancel_selection(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import (
            PatientAppointment,
            parse_appointment_selection,
        )

        candidates_raw = ctx.collected_facts.get("_candidates", [])
        assert isinstance(candidates_raw, list)
        candidates = [
            PatientAppointment(
                appointment_id=c["appointment_id"],
                service_name=c["service_name"],
                resource_name=c["resource_name"],
                start_at=datetime.fromisoformat(c["start_at"]),
                price=c.get("price"),
                status=c["status"],
                pending_action_id=c["pending_action_id"],
                version=c["version"],
                service_id=c["service_id"],
                resource_id=c["resource_id"],
            )
            for c in candidates_raw
        ]

        selected = parse_appointment_selection(user_message, candidates)
        if selected is None:
            return self._fact_turn(
                ctx,
                user_message,
                "I didn't understand. Please reply with the number of the appointment.",
                safety,
                [],
            )

        timezone = str(ctx.collected_facts.get("_business_timezone", "Asia/Kolkata"))
        return await self._create_cancel_proposal(
            ctx, user_message, actor, safety, selected, timezone
        )

    async def _create_cancel_proposal(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
        appt: object,
        timezone: str,
    ) -> ConversationTurn:
        from fonely.domain.appointments.commands import (
            CreatePendingAppointmentCancellationCommand,
        )
        from fonely.services.conversation_tools import (
            PatientAppointment,
            format_confirmation_summary,
        )

        assert isinstance(appt, PatientAppointment)

        proposal = await self._appointment_service.create_cancellation_proposal(  # type: ignore[attr-defined]
            CreatePendingAppointmentCancellationCommand(
                actor=actor,
                appointment_id=appt.appointment_id,
                expected_appointment_version=appt.version,
                reason_code=None,
                expires_at=utcnow() + timedelta(minutes=15),
                idempotency_key=f"conv-{ctx.conversation_id}-cancel-{appt.appointment_id}",
            )
        )
        ctx.proposal_id = proposal.pending_action_id
        ctx.proposal_version = proposal.version
        ctx.collected_facts["_target_appointment_id"] = appt.appointment_id

        summary = format_confirmation_summary(
            appt.service_name, appt.resource_name, appt.start_at, appt.price, timezone
        )
        ctx.transition(ConversationState.AWAITING_CONFIRMATION)
        return self._fact_turn(
            ctx,
            user_message,
            f"Cancel your {summary}? Say yes to confirm.",
            safety,
            [],
        )

    async def _handle_reschedule_intent(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import (
            format_appointment_list,
            get_business_context,
            get_patient_appointments,
        )

        ctx.transition(ConversationState.RESCHEDULE_SELECTION)
        ctx.collected_facts["_operation"] = "reschedule"

        biz = await get_business_context(actor.business_id, self._session)
        timezone = biz.timezone if biz else "Asia/Kolkata"
        ctx.collected_facts["_business_timezone"] = timezone

        appointments = await get_patient_appointments(
            actor.business_id, actor.normalized_phone, self._session
        )

        if not appointments:
            ctx.transition(ConversationState.ENDED)
            return self._fact_turn(
                ctx, user_message, "You don't have any upcoming appointments.", safety, []
            )

        if len(appointments) == 1:
            appt = appointments[0]
            return self._select_reschedule_appointment(ctx, user_message, safety, appt, timezone)

        ctx.collected_facts["_candidates"] = [
            {
                "appointment_id": a.appointment_id,
                "service_name": a.service_name,
                "resource_name": a.resource_name,
                "start_at": a.start_at.isoformat(),
                "version": a.version,
                "pending_action_id": a.pending_action_id,
                "service_id": a.service_id,
                "resource_id": a.resource_id,
                "price": a.price,
                "status": a.status,
            }
            for a in appointments
        ]
        listing = format_appointment_list(appointments, timezone)
        return self._fact_turn(
            ctx,
            user_message,
            f"Which appointment would you like to reschedule?\n{listing}",
            safety,
            [],
        )

    async def _handle_reschedule_selection(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import (
            PatientAppointment,
            parse_appointment_selection,
        )

        candidates_raw = ctx.collected_facts.get("_candidates", [])
        assert isinstance(candidates_raw, list)
        candidates = [
            PatientAppointment(
                appointment_id=c["appointment_id"],
                service_name=c["service_name"],
                resource_name=c["resource_name"],
                start_at=datetime.fromisoformat(c["start_at"]),
                price=c.get("price"),
                status=c["status"],
                pending_action_id=c["pending_action_id"],
                version=c["version"],
                service_id=c["service_id"],
                resource_id=c["resource_id"],
            )
            for c in candidates_raw
        ]

        selected = parse_appointment_selection(user_message, candidates)
        if selected is None:
            return self._fact_turn(
                ctx,
                user_message,
                "I didn't understand. Please reply with the number of the appointment.",
                safety,
                [],
            )

        timezone = str(ctx.collected_facts.get("_business_timezone", "Asia/Kolkata"))
        return self._select_reschedule_appointment(ctx, user_message, safety, selected, timezone)

    def _select_reschedule_appointment(
        self,
        ctx: ConversationContext,
        user_message: str,
        safety: SafetyClassification,
        appt: object,
        timezone: str,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import PatientAppointment

        assert isinstance(appt, PatientAppointment)
        ctx.collected_facts["_target_appointment_id"] = appt.appointment_id
        ctx.collected_facts["_target_appointment_version"] = appt.version
        ctx.collected_facts["service_id"] = appt.service_id
        ctx.collected_facts["service_name"] = appt.service_name
        ctx.collected_facts["resource_id"] = appt.resource_id
        ctx.collected_facts["resource_name"] = appt.resource_name
        ctx.collected_facts["customer_phone"] = str(ctx.collected_facts.get("customer_phone", ""))

        ctx.transition(ConversationState.FACT_COLLECTION)
        return self._fact_turn(
            ctx,
            user_message,
            "When would you like to reschedule to? Please tell me the new date and time.",
            safety,
            ["start_at"],
        )

    async def _check_availability_and_propose(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        biz: object,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.services.conversation_tools import (
            BusinessContext,
            format_confirmation_summary,
        )

        assert isinstance(biz, BusinessContext)
        start_at = ctx.collected_facts["start_at"]
        service_id: int = ctx.collected_facts["service_id"]  # type: ignore[assignment]
        resource_id: int = ctx.collected_facts["resource_id"]  # type: ignore[assignment]

        svc = next((s for s in biz.services if s.id == service_id), None)
        if svc is None:
            return self._fact_turn(
                ctx,
                user_message,
                "Service not found. Please try again.",
                safety,
                ["service_id"],
            )

        ctx.transition(ConversationState.AVAILABILITY_CHECK)

        from datetime import datetime

        from fonely.domain.booking.orchestrator import BookingOrchestrator

        assert isinstance(start_at, datetime)
        operation = ctx.collected_facts.get("_operation", "book")
        exclude_appointment_id: int | None = None
        if operation == "reschedule":
            target_id = ctx.collected_facts.get("_target_appointment_id")
            assert isinstance(target_id, int)
            exclude_appointment_id = target_id

        res = next((r for r in biz.resources if r.id == resource_id), None)
        resource_name = res.name if res else "Doctor"

        # When the patient's time was a bare hour (no am/pm), the other meridiem
        # reading was recorded so availability considers both — a Tamil-speaking
        # patient who means 6 PM must not be offered only morning slots.
        alt_raw = ctx.collected_facts.get("_start_at_alt")
        alt_reading_start = datetime.fromisoformat(alt_raw) if isinstance(alt_raw, str) else None

        orchestrator = BookingOrchestrator(self._session)
        exact_available, offer = await orchestrator.check_and_offer(
            business_id=biz.business_id,
            conversation_id=ctx.conversation_id,
            service_id=service_id,
            service_name=svc.name,
            resource_id=resource_id,
            resource_name=resource_name,
            requested_start=start_at,
            business_timezone=biz.timezone,
            exclude_appointment_id=exclude_appointment_id,
            alt_reading_start=alt_reading_start,
        )

        if not exact_available:
            ctx.state = ConversationState.FACT_COLLECTION
            ctx.booking_attempt += 1
            del ctx.collected_facts["start_at"]
            if offer and offer.slots:
                ctx.collected_facts["_active_offer"] = orchestrator.serialize(offer)
                alt_texts = [s.display_time for s in offer.slots]
                response = (
                    "That exact time isn't available. Nearest slots: "
                    f"{', '.join(alt_texts)}. Which one works?"
                )
            else:
                self._drop_active_offer(ctx)
                response = "That time isn't available. Would you like to try another date?"
            return self._fact_turn(
                ctx,
                user_message,
                response,
                safety,
                ["start_at"],
            )

        if offer:
            ctx.collected_facts["_active_offer"] = orchestrator.serialize(offer)

        # The exact-available offer is authoritative for start_at: when the bare
        # time's ALT reading matched (patient meant 6 PM, 18:00 was the open
        # slot), the proposal must use that slot, not the 06:00 primary reading.
        if exact_available and offer is not None and len(offer.slots) == 1:
            start_at = offer.slots[0].start_at_utc
            ctx.collected_facts["start_at"] = start_at
        ctx.collected_facts.pop("_start_at_alt", None)

        operation = ctx.collected_facts.get("_operation", "book")

        if operation == "reschedule":
            from fonely.domain.appointments.commands import (
                CreatePendingAppointmentRescheduleCommand,
            )

            target_appt_id: int = ctx.collected_facts["_target_appointment_id"]  # type: ignore[assignment]
            target_appt_version: int = ctx.collected_facts["_target_appointment_version"]  # type: ignore[assignment]
            proposal = await self._appointment_service.create_reschedule_proposal(  # type: ignore[attr-defined]
                CreatePendingAppointmentRescheduleCommand(
                    actor=actor,
                    appointment_id=target_appt_id,
                    expected_appointment_version=target_appt_version,
                    service_id=service_id,
                    resource_id=resource_id,
                    start_at=start_at,
                    expires_at=utcnow() + timedelta(minutes=15),
                    idempotency_key=f"conv-{ctx.conversation_id}-reschedule-{target_appt_id}-a{ctx.booking_attempt}",
                )
            )
        else:
            from fonely.domain.appointments.commands import (
                CreatePendingAppointmentCommand,
            )

            proposal = await self._appointment_service.create_proposal(  # type: ignore[attr-defined]
                CreatePendingAppointmentCommand(
                    actor=actor,
                    service_id=service_id,
                    resource_id=resource_id,
                    start_at=start_at,
                    customer_phone=str(
                        ctx.collected_facts.get("customer_phone", actor.normalized_phone)
                    ),
                    customer_name=ctx.collected_facts.get("customer_name"),  # type: ignore[arg-type]
                    reason=None,
                    call_id=None,
                    expires_at=utcnow() + timedelta(minutes=15),
                    idempotency_key=f"conv-{ctx.conversation_id}-a{ctx.booking_attempt}",
                )
            )

        ctx.proposal_id = proposal.pending_action_id
        ctx.proposal_version = proposal.version

        ctx.transition(ConversationState.PROPOSAL_PRESENTED)

        resource_name = str(ctx.collected_facts.get("resource_name", ""))
        service_name = str(ctx.collected_facts.get("service_name", ""))
        summary = format_confirmation_summary(
            service_name, resource_name, start_at, svc.price, biz.timezone
        )
        if operation == "reschedule":
            response = f"Move your appointment to {summary}? Say yes to confirm."
        else:
            response = f"I've found a slot: {summary}. Shall I book this?"

        ctx.transition(ConversationState.AWAITING_CONFIRMATION)
        return self._fact_turn(ctx, user_message, response, safety, [])

    async def _handle_confirmation(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        operation = ctx.collected_facts.get("_operation", "book")
        decision = detect_confirmation(user_message)

        if decision == "negative":
            if operation == "cancel":
                ctx.transition(ConversationState.ENDED)
                return self._fact_turn(
                    ctx, user_message, "Okay, your appointment is unchanged.", safety, []
                )
            ctx.transition(ConversationState.FACT_COLLECTION)
            ctx.booking_attempt += 1
            ctx.proposal_id = None
            ctx.proposal_version = None
            # The rejected slot's selection pointers are now stale — the caller
            # said no to THAT slot. Keep _active_offer (they may pick another of
            # its slots) but drop the pointers so a no-reselect turn does not
            # leak a selection at a refused slot into the persisted row. Reselect
            # overwrites them anyway, so this only bites the no-reselect case.
            self._clear_selection_pointers(ctx)
            # A rejection that ALSO names a new time is a correction ("no no,
            # make it 6 pm"). The correction typically names only the time, not
            # the date — the date was already composed into the rejected
            # start_at. Carry that date forward as _pending_date so the
            # replacement time composes with it, then re-extract and go straight
            # to proposing the corrected slot instead of asking a question we
            # already have the answer to.
            from fonely.services.conversation_tools import (
                BusinessContext,
                get_business_context,
            )

            biz = await get_business_context(ctx.business_id, self._session)
            prior_start = ctx.collected_facts.pop("start_at", None)
            if isinstance(biz, BusinessContext):
                if isinstance(prior_start, datetime):
                    from zoneinfo import ZoneInfo

                    prior_local = prior_start.astimezone(ZoneInfo(biz.timezone))
                    ctx.collected_facts["_pending_date"] = prior_local.date().isoformat()
                self._extract_datetime(ctx, user_message, biz.timezone)
                if "start_at" in ctx.collected_facts and not self._identify_missing_facts(ctx):
                    return await self._check_availability_and_propose(
                        ctx, user_message, actor, biz, safety
                    )
                ctx.collected_facts.pop("_pending_date", None)
            return self._fact_turn(
                ctx,
                user_message,
                "No problem! Would you like a different time?",
                safety,
                self._identify_missing_facts(ctx),
            )

        if decision == "ambiguous":
            action_word = {"cancel": "cancel", "reschedule": "reschedule"}.get(
                str(operation), "book"
            )
            return self._fact_turn(
                ctx,
                user_message,
                f"Could you confirm — should I go ahead and {action_word} this? "
                "Please say yes or no.",
                safety,
                [],
            )

        if ctx.proposal_id is None:
            return self._fact_turn(
                ctx,
                user_message,
                "Something went wrong. Let's start over.",
                safety,
                self._identify_missing_facts(ctx),
            )

        if operation == "cancel":
            return await self._confirm_cancellation(ctx, user_message, actor, safety)
        if operation == "reschedule":
            return await self._confirm_reschedule(ctx, user_message, actor, safety)
        return await self._confirm_booking(ctx, user_message, actor, safety)

    async def _confirm_booking(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.domain.appointments.commands import ConfirmPendingAppointmentCommand
        from fonely.domain.appointments.errors import AppointmentDomainError
        from fonely.domain.appointments.results import (
            PreCommitAppointmentFailure,
            PreCommitAppointmentSuccess,
        )

        assert ctx.proposal_id is not None
        try:
            result = await self._appointment_service.confirm_and_commit(  # type: ignore[attr-defined]
                ConfirmPendingAppointmentCommand(
                    actor=actor,
                    pending_action_id=ctx.proposal_id,
                    expected_version=ctx.proposal_version or 1,
                )
            )
        except (AppointmentDomainError, ValueError):
            ctx.state = ConversationState.FACT_COLLECTION
            ctx.booking_attempt += 1
            ctx.collected_facts.pop("start_at", None)
            ctx.proposal_id = None
            ctx.proposal_version = None
            return self._fact_turn(
                ctx,
                user_message,
                "That time is no longer available. Would you like to try another time?",
                safety,
                ["start_at"],
            )

        if isinstance(result, PreCommitAppointmentFailure):
            ctx.state = ConversationState.FACT_COLLECTION
            ctx.booking_attempt += 1
            ctx.collected_facts.pop("start_at", None)
            ctx.proposal_id = None
            ctx.proposal_version = None
            return self._fact_turn(
                ctx,
                user_message,
                "That slot is no longer available. Would you like to try another time?",
                safety,
                ["start_at"],
            )

        assert isinstance(result, PreCommitAppointmentSuccess)

        ctx.transition(ConversationState.CONFIRMED)
        ctx.transition(ConversationState.COMPLETED)
        return self._fact_turn(
            ctx,
            user_message,
            f"Your appointment is confirmed! "
            f"Appointment ID: {result.appointment.appointment_id}. "
            f"See you at the clinic!",
            safety,
            [],
        )

    async def _confirm_cancellation(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.domain.appointments.commands import (
            ConfirmPendingAppointmentCancellationCommand,
        )

        assert ctx.proposal_id is not None
        await self._appointment_service.confirm_cancellation(  # type: ignore[attr-defined]
            ConfirmPendingAppointmentCancellationCommand(
                actor=actor,
                pending_action_id=ctx.proposal_id,
                expected_version=ctx.proposal_version or 1,
            )
        )

        ctx.transition(ConversationState.CONFIRMED)
        ctx.transition(ConversationState.COMPLETED)
        return self._fact_turn(
            ctx,
            user_message,
            "Your appointment has been cancelled. The clinic has been notified.",
            safety,
            [],
        )

    async def _confirm_reschedule(
        self,
        ctx: ConversationContext,
        user_message: str,
        actor: ActorContext,
        safety: SafetyClassification,
    ) -> ConversationTurn:
        from fonely.domain.appointments.commands import (
            ConfirmPendingAppointmentRescheduleCommand,
        )
        from fonely.domain.appointments.errors import AppointmentDomainError

        assert ctx.proposal_id is not None
        try:
            result = await self._appointment_service.confirm_reschedule(  # type: ignore[attr-defined]
                ConfirmPendingAppointmentRescheduleCommand(
                    actor=actor,
                    pending_action_id=ctx.proposal_id,
                    expected_version=ctx.proposal_version or 1,
                )
            )
        except (AppointmentDomainError, ValueError):
            ctx.state = ConversationState.FACT_COLLECTION
            ctx.booking_attempt += 1
            ctx.collected_facts.pop("start_at", None)
            ctx.proposal_id = None
            ctx.proposal_version = None
            return self._fact_turn(
                ctx,
                user_message,
                "That slot isn't available. Would you like to try another time?",
                safety,
                ["start_at"],
            )

        ctx.transition(ConversationState.CONFIRMED)
        ctx.transition(ConversationState.COMPLETED)

        from fonely.services.conversation_tools import format_confirmation_summary

        new_start = result.start_at
        resource_name = str(result.resource_name)
        service_name = str(ctx.collected_facts.get("service_name", ""))
        biz_tz = str(ctx.collected_facts.get("_business_timezone", "Asia/Kolkata"))
        summary = format_confirmation_summary(service_name, resource_name, new_start, None, biz_tz)
        return self._fact_turn(
            ctx,
            user_message,
            f"Your appointment has been rescheduled to {summary}.",
            safety,
            [],
        )

    async def _generate_response(
        self,
        ctx: ConversationContext,
        user_message: str,
        biz: object,
        missing: list[str],
        safety: SafetyClassification,
    ) -> ModelResponse:
        from fonely.services.conversation_tools import BusinessContext

        assert isinstance(biz, BusinessContext)
        services_text = ", ".join(
            f"{s.name} (₹{s.price}, {s.duration_minutes}min)" for s in biz.services
        )
        resources_text = ", ".join(r.name for r in biz.resources)

        system_prompt = (
            f"You are the virtual receptionist for {biz.name}. "
            f"You handle appointment bookings and clinic enquiries. "
            f"Respond in the caller's language (Tamil/English/mixed). "
            f"Use short spoken sentences, not written prose. "
            f"Ask one question at a time. Never invent clinic info. "
            f"Available services: {services_text}. "
            f"Available dentists: {resources_text}. "
        )

        if missing:
            system_prompt += (
                f"The customer still needs to provide: "
                f"{', '.join(missing)}. "
                f"Ask about ONE missing item naturally."
            )

        history: list[dict[str, str]] = []
        for t in ctx.turns[-6:]:
            history.append({"role": "user", "content": t.user_message})
            history.append({"role": "assistant", "content": t.assistant_response})
        history.append({"role": "user", "content": user_message})

        return await self._model.complete(
            system_prompt=system_prompt,
            messages=history,
            temperature=0.3,
            max_tokens=300,
        )

    async def _persist_turn(self, conversation_id: str, turn: ConversationTurn) -> None:
        from fonely.services.conversation_persistence import (
            ConversationPersistenceService,
        )

        ctx = _CONVERSATIONS.get(conversation_id)
        if ctx is None:
            return

        has_critical_state = ctx.proposal_id is not None or turn.state in (
            ConversationState.CONFIRMED,
            ConversationState.COMPLETED,
        )

        try:
            persistence = ConversationPersistenceService(self._session)
            exists = await persistence.exists(conversation_id)
            if not exists:
                if has_critical_state:
                    from fonely.core.metrics import metrics

                    metrics.increment(
                        "conversation_critical_state_unpersisted",
                        {"business_id": str(ctx.business_id)},
                    )
                    logger.warning(
                        "critical_state_not_persisted: conversation=%s",
                        conversation_id,
                    )
                return
            async with self._session.begin_nested():
                await persistence.save_turn(ctx, turn)
        except Exception:
            if has_critical_state:
                _CONVERSATIONS.pop(conversation_id, None)
                raise
            logger.debug("conversation_persist_skipped", exc_info=True)

    def _log_turn(self, turn: ConversationTurn, start_time: float) -> None:
        latency = round((time.monotonic() - start_time) * 1000)

        from fonely.core.metrics import metrics

        bid = str(turn.business_id)
        metrics.increment(
            "conversation_turns_total",
            {"business_id": bid, "state": turn.state.value, "intent": turn.intent.value},
        )
        metrics.observe("conversation_turn_duration_ms", latency, {"business_id": bid})
        if turn.safety_classification != "administrative":
            metrics.increment(
                "safety_classifications_total",
                {"classification": turn.safety_classification},
            )

        logger.info(
            "conversation_turn",
            extra={
                "event": "conversation_turn",
                "conversation_id": turn.conversation_id,
                "turn_id": turn.turn_id,
                "turn_number": len(
                    _CONVERSATIONS.get(
                        turn.conversation_id,
                        ConversationContext(business_id=0),
                    ).turns
                ),
                "state": turn.state.value,
                "intent": turn.intent.value,
                "safety": turn.safety_classification,
                "missing_facts": turn.missing_facts,
                "has_proposal": turn.proposal_id is not None,
                "latency_ms": latency,
            },
        )

    def _fact_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        response: str,
        safety: SafetyClassification,
        missing: list[str],
    ) -> ConversationTurn:
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=response,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=missing,
            proposal_id=ctx.proposal_id,
            proposal_version=ctx.proposal_version,
            intent=safety.intent,
            safety_classification=safety.classification,
        )
        ctx.turns.append(turn)
        return turn

    def _make_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        response: str,
        intent: ConversationIntent,
        classification: str,
    ) -> ConversationTurn:
        return ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=response,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=[],
            intent=intent,
            safety_classification=classification,
        )

    def _escalate_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        safety: SafetyClassification,
        escalation_message: str,
    ) -> ConversationTurn:
        if ctx.can_transition(ConversationState.ESCALATED):
            ctx.transition(ConversationState.ESCALATED)
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=escalation_message,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=[],
            intent=safety.intent,
            safety_classification=safety.classification,
        )
        ctx.turns.append(turn)
        return turn

    def _end_turn(
        self,
        ctx: ConversationContext,
        user_message: str,
        response: str,
        intent: ConversationIntent,
        classification: str,
    ) -> ConversationTurn:
        if ctx.can_transition(ConversationState.ENDED):
            ctx.transition(ConversationState.ENDED)
        turn = ConversationTurn(
            turn_id=str(uuid.uuid4()),
            conversation_id=ctx.conversation_id,
            business_id=ctx.business_id,
            state=ctx.state,
            user_message=user_message,
            assistant_response=response,
            collected_facts=dict(ctx.collected_facts),
            missing_facts=[],
            intent=intent,
            safety_classification=classification,
        )
        ctx.turns.append(turn)
        return turn

    async def _persist_voice_callback(
        self,
        ctx: ConversationContext,
        actor: ActorContext,
        reason_code: str,
        attempted_candidates: list[str],
    ) -> None:
        """Leave a durable callback so a voice give-up is followed up, not lost.

        Called only on the VOICE terminal give-up (the caller is on a live call
        and we could not disambiguate). Persists the partial booking facts a
        human needs to call back and finish the booking.

        TENANT SAFETY: business_id and the authoritative caller identity come
        from the TRUSTED actor context (PendingActionService.create binds
        business_id and initiated_by from command.actor, never the payload). The
        payload's caller_phone is only the number to dial back, taken from the
        verified actor — not a model/caller-supplied value. A callback therefore
        cannot be created under another tenant's scope.

        Best-effort within its own savepoint: a callback that fails to persist
        must not turn the give-up itself into an error to the caller (they still
        get the honest give-up message). But the FAILURE is logged loudly — a
        silently-dropped callback is the "absence reads as success" trap.

        NOTE (follow-on, not in this scope): the callback is PERSISTED but not
        yet surfaced to any owner/worklist consumer. It is durable but invisible
        until such a consumer exists — bounded by expires_at so it is at least
        compliance-safe (PII self-expires) while unworked. A notification/
        worklist consumer is required before this is a real production follow-up.
        """
        from datetime import UTC

        from fonely.domain.pending_actions.commands import CreatePendingActionCommand
        from fonely.services.pending_actions import MAX_EXPIRY_HORIZON, PendingActionService

        service_id = ctx.collected_facts.get("service_id")
        service_name = ctx.collected_facts.get("service_name")
        start_at = ctx.collected_facts.get("start_at")
        target_date: str | None = None
        if isinstance(start_at, datetime):
            from zoneinfo import ZoneInfo

            biz_tz = ctx.collected_facts.get("_business_timezone")
            tz = ZoneInfo(biz_tz) if isinstance(biz_tz, str) else UTC
            target_date = start_at.astimezone(tz).date().isoformat()

        now = utcnow()
        payload: dict[str, object] = {
            "schema_version": 1,
            "action_type": PendingActionType.CALLBACK.value,
            "data": {
                "reason_code": reason_code,
                # From the TRUSTED actor, never a model field — the number we dial.
                "caller_phone": actor.normalized_phone,
                "service_id": service_id if isinstance(service_id, int) else None,
                "service_name": service_name if isinstance(service_name, str) else None,
                "target_date": target_date,
                "attempted_candidates": attempted_candidates[:20],
                "requested_at": now.isoformat(),
            },
        }
        # PERSIST first (the durable record), NOTIFY second (best-effort push).
        # These are intentionally NOT one atomic unit. The callback ROW is the
        # thing that matters — B's owner worklist surfaces it and the retention
        # sweep bounds its PII regardless of whether the push ever fires. The
        # notification is an enhancement on top. So:
        #   * persist fails -> no callback, give-up proceeds (logged);
        #   * persist succeeds, notify fails -> the callback SURVIVES and the
        #     owner can still PULL it from the worklist (#41-B); the push is
        #     dropped + logged. Degrading to "queryable but not pushed" (exactly
        #     B's guarantee) is strictly better than losing the durable record to
        #     a transient WhatsApp-channel error.
        # Because notify runs only AFTER the callback is persisted, there is never
        # a notified-but-unpersisted split; the only tolerated split is
        # persisted-but-unpushed, which is a graceful degradation, not an
        # inconsistency. Each stage is its own savepoint so a failure in one does
        # not poison the surrounding conversation transaction.
        result_id: int | None = None
        try:
            async with self._session.begin_nested():
                service = PendingActionService(self._session)
                result = await service.create(
                    CreatePendingActionCommand(
                        actor=actor,
                        action_type=PendingActionType.CALLBACK,
                        payload_schema_version=1,
                        payload=payload,
                        # expires_at is the PA-LIFECYCLE staleness marker (this
                        # callback is a stale actionable item after a day), NOT
                        # the PII bound. Pending actions cap at MAX_EXPIRY_HORIZON
                        # (24h) and expiry only flips status→EXPIRED, never
                        # deletes. The PII bound is the retention sweep alone
                        # (CALLBACK_TTL_DAYS=90d, data_retention._cleanup_callbacks)
                        # — this codebase's only row-deleter, which sweeps a
                        # callback whether it is worked, unworked, or expired.
                        expires_at=now + MAX_EXPIRY_HORIZON,
                        idempotency_key=f"callback-{ctx.conversation_id}-{reason_code}",
                    )
                )
                result_id = result.id
        except Exception:
            # A dropped callback must not fail the give-up, but must be visible.
            logger.warning(
                "voice_callback_persist_failed",
                exc_info=True,
                extra={"business_id": actor.business_id, "reason_code": reason_code},
            )
            return

        try:
            from fonely.services.notifications import NotificationService

            service_name = ctx.collected_facts.get("service_name")
            async with self._session.begin_nested():
                await NotificationService(self._session).create_callback_notification(
                    business_id=actor.business_id,
                    callback_pending_action_id=result_id,
                    caller_phone=actor.normalized_phone,
                    reason_code=reason_code,
                    service_name=service_name if isinstance(service_name, str) else None,
                    target_date=target_date,
                    attempted_candidates=attempted_candidates[:20],
                )
        except Exception:
            # The push failed (e.g. unconfigured WhatsApp channel). The callback
            # ROW is already persisted and re-offerable via the owner worklist, so
            # this degrades to "queryable but not pushed" — B's guarantee — not a
            # lost follow-up. Loud log is the operator's signal to fix the channel.
            logger.warning(
                "voice_callback_notify_failed",
                exc_info=True,
                extra={
                    "business_id": actor.business_id,
                    "callback_pending_action_id": result_id,
                    "reason_code": reason_code,
                },
            )


def get_conversation(
    conversation_id: str,
) -> ConversationContext | None:
    return _CONVERSATIONS.get(conversation_id)


def create_conversation(business_id: int) -> ConversationContext:
    _evict_stale()
    ctx = ConversationContext(business_id=business_id)
    _CONVERSATIONS[ctx.conversation_id] = ctx
    return ctx


_PHONE_INDEX: dict[tuple[int, str], str] = {}


def find_or_create_conversation(
    business_id: int,
    customer_phone: str,
) -> ConversationContext:
    key = (business_id, customer_phone)
    existing_id = _PHONE_INDEX.get(key)
    if existing_id is not None:
        ctx = _CONVERSATIONS.get(existing_id)
        if ctx is not None and ctx.state not in (
            ConversationState.COMPLETED,
            ConversationState.ENDED,
        ):
            return ctx

    ctx = create_conversation(business_id)
    _PHONE_INDEX[key] = ctx.conversation_id
    return ctx


async def find_or_create_conversation_persistent(
    business_id: int,
    customer_phone: str,
    session: object,
) -> ConversationContext:
    key = (business_id, customer_phone)
    existing_id = _PHONE_INDEX.get(key)
    if existing_id is not None:
        ctx = _CONVERSATIONS.get(existing_id)
        if ctx is not None and ctx.state not in (
            ConversationState.COMPLETED,
            ConversationState.ENDED,
        ):
            return ctx

    try:
        from fonely.services.conversation_persistence import ConversationPersistenceService

        persistence = ConversationPersistenceService(session)  # type: ignore[arg-type]
        ctx = await persistence.load_or_create(business_id, customer_phone)
        _CONVERSATIONS[ctx.conversation_id] = ctx
        _PHONE_INDEX[key] = ctx.conversation_id
        return ctx
    except Exception:
        logger.debug("persistent_find_or_create_failed", exc_info=True)
        return find_or_create_conversation(business_id, customer_phone)
