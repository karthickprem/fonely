# Human Review Workflow for Fonely Evaluations

## Purpose

Synthetic cases are requirements drafts, not scoring truth. QA.3 currently contains 211 structurally valid cases and 377 turns, but the cases remain language-synthetic, domain-unreviewed, and pilot-untested unless their provenance fields state otherwise. A case becomes eligible for a provider-quality gate only after its language and domain expectations are reviewed independently. Pilot validation is a third, separate status.

## Review status fields

### Language review

- `synthetic`: generated or edited without native-speaker approval.
- `native_reviewed`: a native speaker approved the utterance, expected interpretation, code-switching, register, and cultural plausibility.

### Domain review

- `unreviewed`: product or safety expectations have not been approved.
- `product_reviewed`: a product/domain owner approved tool, argument, outcome, write-policy, database-effect, and response constraints.
- `clinician_reviewed`: a qualified clinician approved medical escalation and prohibited-advice constraints.

### Pilot validation

- `untested`: not observed against a real call or approved pilot simulation.
- `passed`: observed behavior matched the reviewed case.
- `failed`: observed behavior did not match; preserve the failure result until triaged.

## Tamil native-speaker review

Tamil is the first pilot language and should be reviewed before other regional-language expansion.

For each `ta-IN` case, the reviewer checks:

1. Tamil script spelling and grammar where applicable.
2. Romanized Tamil readability without requiring one rigid transliteration standard.
3. Natural Tanglish/code-switching for Chennai callers.
4. Quantity, price, date, time, negation, and correction meaning.
5. Polite and informal registers.
6. Whether the expected intent/tool arguments preserve the caller's meaning.
7. Whether the agent response constraints require natural Tamil rather than literal translation.

Promotion rule: set `language_review_status` to `native_reviewed` only after all checks pass. Record reviewer role and review date outside the corpus; do not store reviewer identity or contact information in JSONL.

## Medical/clinical review

Every `medical_safety` case must be reviewed by a qualified clinician before it gates releases.

The reviewer checks:

1. No diagnosis or implied diagnosis.
2. No medication, dosage, procedure, or treatment recommendation.
3. No claim that delaying care is safe or unsafe.
4. Escalation urgency is proportionate to the described symptoms.
5. Triage questions are minimal, approved, and used only to route care.
6. Child, trauma, swelling, bleeding, severe pain, and fever scenarios use approved escalation language.
7. The system offers booking or clinician callback without representing that as medical advice.

Promotion rule: set `domain_review_status` to `clinician_reviewed`. Language review remains independent.

## Transactional product review

For pending action, inventory, appointment, authorization, voice runtime, and provider routing cases, a product/domain reviewer checks:

1. `expected_tool` is the correct lifecycle-safe public tool.
2. Arguments contain caller-provided transaction facts only—not trusted tenant identity or verified role.
3. Proposal and confirmation are separate operations.
4. Outcome/error code is deterministic.
5. Write policy matches the database effect.
6. Forbidden behavior protects tenant isolation, concurrency, inventory, scheduling, and confirmation invariants.
7. Internal operations (`begin_commit`, `complete_commit`, `fail_commit`, `internal_get`, `internal_get_active`) never appear as LLM-callable tools.

Promotion rule: set `domain_review_status` to `product_reviewed`.

## Review evidence and disagreements

- Use a review ticket or immutable review record containing case IDs, reviewer role, date, and decision.
- Do not add reviewer/customer PII to corpus files.
- If reviewers disagree, keep the case unreviewed and record both interpretations in the review ticket.
- Any semantic corpus change resets the affected review status to unreviewed/synthetic unless the reviewer approves the edit.

## Pilot validation

Pilot validation occurs only after language and domain review. For each observed call:

1. Map it to a reviewed case or add a new synthetic case.
2. Compare actual tool, arguments, outcome, write policy, database effect, and spoken constraints.
3. Mark `passed` only when all reviewed requirements are met.
4. Mark `failed` when any reviewed requirement fails; do not overwrite failures to make aggregate metrics pass.
5. Store call evidence in the approved telemetry/evaluation system, not in JSONL.

## Required review order

1. Product review of the lifecycle-safe tool contract.
2. Product review of critical transactional/authorization cases.
3. Clinical review of all medical safety cases.
4. Native Tamil review for Chennai pilot.
5. Pilot validation.
6. Native Hindi/Telugu/Kannada/Malayalam review before those locales become blocking gates.
