# Independent Engineering Review — Fonely

**To:** AI Cofounder (product/technical architecture, work allocation, integration gates, readiness)
**Basis:** Direct inspection of `main` at `4c765c6`, 2026-08-10. Repo history, source tree, migration graph, branch topology, prompt construction, channel adapters. No test execution (machine load average 30; per project policy full suites were not run).
**Standing:** External reviewer. Karthick has read this. Allocation decisions remain yours; the founder dependency in §5 is his alone.

---

## Verdict

The foundation is better than most seed-stage AI products. The integration machine works. The effort is aimed at the wrong risk, and the single highest customer-visible defect in the repository has been open since the live Tamil test while twenty rounds of hardening went into a migration downgrade path on tables with zero production rows.

Recommended action: freeze schema hardening, fix the conversation prompt today, redirect capacity to the two unproven paths (voice, real user).

---

## 1. Findings

### F1 — P0, still live in main: the model has no date grounding

`backend/src/fonely/services/conversation.py:1027` constructs the system prompt for every patient turn. It contains: business name, services with prices, resource names. It contains **no date, no time, no day of week, and no clinic hours.**

```python
system_prompt = (
    f"You are the virtual receptionist for {biz.name}. "
    ...
    f"Ask one question at a time. Never invent clinic info. "
    f"Available services: {services_text}. "
    f"Available dentists: {resources_text}. "
)
```

Consequences:

1. When a caller says "நாளை" or "next Tuesday," the generating model does not know what date that is. The deterministic extractor at `conversation.py:358` resolves some relative dates correctly against `clinic_tz`, so the *committed row* may be right — but the *sentence the patient hears* is produced by a model that is guessing. Divergence between what was said and what was committed is a trust defect, and it is invisible to every PostgreSQL test currently in the suite, because those tests assert on the committed row.
2. The prompt instructs the model to "never invent clinic info" while withholding the clinic info it needs. Invention is the only available move. This is the misleading-generic-hours half of the same defect.

This is the exact failure the live Tamil/Tanglish test recorded. It is unfixed. **Severity: this is the product.** Every other guarantee in the system is downstream of the patient believing what the receptionist told them.

Fix is small and local: inject trusted clinic-local `now`, day of week, and the resolved schedule for the relevant window; state explicitly that availability comes only from tool results and must never be asserted from the prompt. One file, roughly twenty lines, plus a conversational regression asserting the model's *reply* agrees with the committed slot.

**Ownership note:** `conversation.py` is one of the four most-churned files in the repo (20 revisions) and sits on the boundary between Dev1's domain correctness and Dev3's channel/runtime scope. I could not determine from the branch topology who currently owns it. Ambiguous ownership on the highest-value file is itself a finding — assign it explicitly before the fix.

### F2 — The differentiating path is a stub

`backend/src/fonely/api/channels/exotel.py:107`:

```python
async def audio_stream(websocket: WebSocket) -> None:
    """For now: accept, log, and close.
    Actual audio processing will be wired to Pipecat pipeline by Dev4."""
```

It accepts frames and discards them. The voice path — the stated moat, the reason Tamil quality matters, the thing Exotel and Cartesia and the STT bake-off all exist to serve — has never carried a call. No human has completed a booking by speaking to this system over a telephone line.

### F3 — Effort allocation

Nine days on `main` (2026-08-01 → 2026-08-09), 268 commits:

| type | count |
|------|-------|
| fix | 163 |
| feat | 50 |
| test | 31 |
| docs | 12 |
| other | 12 |

Most-revised files: `test_migration_parity.py` (20), `test_inventory_order_migration_postgres.py` (20), `services/conversation.py` (20), `services/appointments.py` (20), `api/channels/whatsapp.py` (19).

Nine of the last ten commits harden the downgrade path of the durable-inbox migration. `0014_inbound_event_claim_and_dedup_removal` is a revision you write after observing production traffic, not in week two. Fourteen migrations in nine days, against zero production rows.

Set against F1 and F2: the system has dead-letter queues, circuit breakers, backup/restore proofs, PII-safe logging, lossy-downgrade tombstone guards, and `pg_stat_activity` contender barriers — and a receptionist that does not know what day it is.

A lossy downgrade on an empty table costs a `pg_restore` and nothing else. A tenant-isolation leak between two clinics would end the company. Those received comparable investment. Rigor is not the problem; **uniform** rigor is.

### F4 — The readiness artifact is stale, and it is your artifact

`docs/STATUS.md` is stamped 2026-08-03 and reports `Alembic head: 0008`. Actual head is `0014` — six migrations behind. It lists Dev4 on "Pipecat voice lab" and Dev3 on "session commit fix," neither of which reflects current allocation. `docs/daily/` stops at 2026-08-02.

You own readiness assessment. The document that communicates readiness has drifted a week and six migrations out of date while the codebase it describes turned over 268 commits. If STATUS.md is not regenerated as a gate artifact — mechanically, from the repo, at every integration — then readiness is being asserted from memory rather than evidence, which is the specific failure mode the evidence ladder exists to prevent.

---

## 2. What is working, and should not be touched

Stated plainly because the rest of this document is critical and the following is genuinely strong:

- **The commit discipline.** 113 branches, 98 merged into main, 15 open, 6 with commits in the last 48 hours. Coherent revision graph, non-overlapping worktrees, no competing Alembic heads. Roughly nine stale branches. This is a functioning integration machine, not a thrashing one.
- **The core architectural commitment.** *The model proposes; the deterministic engine commits; PostgreSQL is authoritative.* Most teams building an AI receptionist hand the booking tool to the LLM and discover hallucinated appointments in production. This decision is correct and load-bearing and should survive every schedule pressure that comes.
- **Tenant safety.** Scoping by trusted `business_id`, idempotency backed by database uniqueness, deterministic lock ordering, concurrency proven with independent sessions. This is the class of invariant that deserves level 4–9 evidence, and it has it.
- **India-first speech posture.** Tamil is where general-purpose vendors are weakest. Being good there is defensible in a way that being good at English never would be.

The uncomfortable version of §1 is not that the machine is broken. It is that a precise, well-governed machine is executing accurately against a low-value target. A thrashing team is fixable with process. This is not, and more process makes it worse.

---

## 3. Structural diagnosis

The advice "collapse the evidence ladder" is too shallow, and I want to state the underlying mechanism because you are the component that can install the compensating control.

The engineering organization is entirely AI agents. Agent labor has a property human labor does not: it is fast, tireless, locally competent, and **free of the felt cost that normally forces prioritization.**

Four humans would have revolted somewhere around the fifteenth revision of `test_migration_parity.py`. Someone would have said out loud: *why are we doing this, nobody uses this thing.* Agents never say that. Every individual hardening step is defensible in isolation, so the loop runs until something external stops it. Each process failure produces a new governance artifact; each governance artifact produces new surface to comply with; compliance work is indistinguishable from progress in a commit log. The 3.3:1 fix-to-feature ratio and the accumulated corpus of process corrections are not evidence of insufficient discipline. They are the **equilibrium state of agent labor with no external forcing function.**

The implication is uncomfortable and important: process cannot fix this, because process is the symptom presenting as the cure. The only mechanism that reliably reprioritizes engineering is a real user whose behavior makes some work obviously urgent and the rest obviously not. One clinic is not needed for revenue or validation. One clinic is needed because it is the only object in this system with the standing to say *nobody cares about your downgrade path.*

---

## 4. Recommended reallocation

Ordered. Decidable. Yours to allocate.

1. **Fix `conversation.py:1027` today.** Trusted clinic-local date/time/day-of-week and resolved hours injected into the prompt; explicit instruction that availability originates only from tool results. Add a regression that asserts the generated reply agrees with the committed slot — not just that the row is correct. Assign ownership of `conversation.py` explicitly first.
2. **Freeze migration and schema hardening on any table with zero production rows.** Land `0015` to unblock Dev1's Exotel work and Dev3's owner-notification path, then stop. Downgrade elegance is revisitable when there is data worth losing.
3. **Redirect Dev2 from migration policy to staging deployment.** Staging is listed as P1-blocked-on-Docker and is the actual gate between here and a pilot. Migration hardening is not.
4. **Wire `exotel.py:107` to the Pipecat pipeline.** Dev4 and Dev3. The gate is not "the websocket carries audio" — it is one real Tamil call, from a real phone number, by a speaker who is not on this team, completing a real booking, reviewed by a native speaker. That is the Real Conversation Quality Gate, and it is the only gate in the ladder that currently gates anything unknown.
5. **Regenerate `docs/STATUS.md` mechanically from the repo** and make regeneration a step in the integration gate.
6. **Collapse the ladder by invariant class.** Levels 4–9 reserved for: tenant isolation, double-booking, hallucinated commitment, money. Levels 1–3 for everything else until a customer's behavior argues otherwise.

### 🚨🚨 KARTHICK NEEDED 🚨🚨

**One clinic, in shadow mode, before the code is ready.** AI drafts the reply, a human reads it and sends it. This is a founder dependency — it requires a customer relationship, and it cannot be unblocked by any allocation you make.

Three days of shadow mode would teach more about real Tanglish intent coverage than another 200 commits of hardening against an imagined user. Until it exists, every fix in this repository is a fix against a guess, and the fix ratio will keep compounding on guesses.

---

## 5. Open questions I could not resolve

**Unit economics.** No per-minute voice cost model exists in the repository — telephony plus STT plus LLM plus TTS against a capped MSME plan. This may be reasoned about outside the repo. If so it belongs in it, because it should gate provider selection. Current provider posture appears driven by Tamil quality and grant availability rather than by margin at the price an independent Chennai clinic will actually pay. If a six-minute call costs more than a day of subscription revenue, that is an architecture input, not a finance detail.

**Interpretation of the fix ratio.** A high fix-to-feature ratio can indicate rigorous self-correction — catching your own defects before customers do — which is desirable. I read these 163 as misallocated based on *what* was fixed (tombstone guards, contender barriers: defenses against load that does not exist). But I inferred intent from commit subjects. `bd122a8 fix(whatsapp): fail closed without verification token` is a genuine security fix, and if a material share of the 163 are of that kind, the picture is healthier than §1 implies. You have the context to make this determination; I do not.

---

## 6. Falsifier

This review is wrong if the pilot is genuinely months out and the real first buyer is multi-location dental chains, where data integrity *is* the sale and procurement will audit it. In that world, front-loading unbreakable correctness is correct sequencing and my recommendation inverts.

`PLAN.md` says independent urban clinics with 1–3 dentists in Chennai. Against that buyer, this review stands. If the buyer has changed, say so and I will re-derive the recommendation — but then `PLAN.md` needs updating too, because half the engineering organization is executing against the document.

---

## Closing

The bones are better than most of what I see at this stage, and the architectural commitment at the center of this system is the right one. The risk is not that you ship something broken. It is that you spend six months building an unbreakable thing nobody has tried to use — and that the defect your own live Tamil test already found stays the least-examined line in the repository while the migration downgrade path receives its twenty-first review.

Fix line 1027 before the next migration.
