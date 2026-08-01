# Pilot Scorecard

Measurable metrics for the Fonely pilot phase (5-10 businesses). All targets
are hypotheses based on assumptions about user behavior and system capability.
They will be revised based on actual pilot data.

---

## Metrics and Thresholds

All targets marked **(hypothesis)** are initial estimates. Adjust after the
first 2 weeks of pilot data.

| # | Metric | Target (hypothesis) | Category |
|---|--------|---------------------|----------|
| 1 | Valid-call completion rate | > 85% | Quality |
| 2 | Correct intent recognition rate | > 90% | Quality |
| 3 | Correct tool selection rate | > 95% | Quality |
| 4 | Wrong transaction rate | < 2% | Safety |
| 5 | Double-booking count | 0 | Safety |
| 6 | Oversell count | 0 | Safety |
| 7 | Owner correction rate | < 10% | Quality |
| 8 | Escalation correctness | > 95% | Quality |
| 9 | P50 response latency | < 1.5s | Performance |
| 10 | P95 response latency | < 3.0s | Performance |
| 11 | Cost per connected minute | < Rs 10 | Unit Economics |
| 12 | Cost per successful transaction | < Rs 15 | Unit Economics |
| 13 | Caller abandonment rate | < 20% | User Experience |
| 14 | Owner satisfaction (1-5 scale) | > 3.5 | Business |
| 15 | Willingness to pay | > 60% of trial users | Business |
| 16 | Renewal intent | > 50% | Business |

---

## Metric Definitions

### 1. Valid-Call Completion Rate

**Definition:** Percentage of calls where the AI provides a definitive outcome
(order placed, appointment booked, enquiry answered, out-of-stock communicated,
or escalated to owner) out of all calls that last more than 10 seconds.

**Data source:** `calls.outcome` column. Exclude calls with `duration_sec < 10`
(accidental dials, network drops). Count outcomes `ordered`, `booked`,
`enquiry`, `out_of_stock`, `escalated` as completed. Count `no_action` and
`dropped` as incomplete.

**Formula:** `completed_calls / (total_calls - calls_under_10s) * 100`

**If threshold not met:** Review call transcripts for the most common failure
patterns. Likely causes: STT misrecognition, LLM failing to identify intent,
caller speaking a language not yet tuned. Prioritize the top failure mode.

### 2. Correct Intent Recognition Rate

**Definition:** Percentage of caller utterances where the LLM correctly
identifies the caller's intent (order, appointment, enquiry, complaint, etc.).

**Data source:** Manual review of call transcripts. Sample at least 50 calls
per week. Compare LLM's chosen tool call against a human-labeled ground truth.

**Formula:** `correct_intents / total_labeled_utterances * 100`

**If threshold not met:** Analyze confusion patterns. Common issues: ambiguous
utterances in regional language, code-switching between languages, domain
vocabulary not in LLM training data. Improve system prompt with examples of
misrecognized patterns.

### 3. Correct Tool Selection Rate

**Definition:** Percentage of LLM tool calls where the correct tool was selected
(e.g., `check_stock` vs `place_order` vs `book_appointment`).

**Data source:** Manual review of call transcripts. For each tool call in the
transcript, verify the tool name matches what a human would have selected.

**Formula:** `correct_tool_calls / total_tool_calls * 100`

**If threshold not met:** Review the tool-calling schema for ambiguity. Add
clarifying descriptions to tool definitions. Add few-shot examples to the
system prompt for commonly confused tool pairs.

### 4. Wrong Transaction Rate

**Definition:** Percentage of completed transactions (orders or appointments)
where the outcome did not match what the caller requested. Includes: wrong
item, wrong quantity, wrong time, wrong service.

**Data source:** Owner correction reports via WhatsApp (owner says "this order
is wrong") and manual transcript review.

**Formula:** `wrong_transactions / total_transactions * 100`

**If threshold not met:** This is a safety-critical metric. If above 2%,
investigate whether the confirmation step is being bypassed or whether the
LLM is confirming details the caller did not agree to. Consider adding a
second confirmation for high-value transactions.

### 5. Double-Booking Count

**Definition:** Number of instances where two appointments are booked for the
same resource at overlapping times.

**Data source:** Database query: for each resource, check for overlapping
`(start_at, end_at)` ranges among appointments with status `held` or
`confirmed`.

**Formula:** Count of overlapping appointment pairs.

**If threshold not met:** This is a P0 defect. The database exclusion constraint
must be deployed immediately. Until then, application-level locking must prevent
concurrent booking of the same slot.

### 6. Oversell Count

**Definition:** Number of instances where inventory was sold beyond available
stock (on_hand_qty went negative, or reserved_qty exceeded on_hand_qty).

**Data source:** Database query: check `inventory_balances` for rows where
`reserved_qty > on_hand_qty` or `on_hand_qty < 0`. Also check
`inventory_movements` for any movement that resulted in `available_after < 0`.

**Formula:** Count of constraint-violating rows.

**If threshold not met:** This is a P0 defect. The check constraints
`ck_inv_on_hand` and `ck_inv_reserved_lte_on_hand` should prevent this at the
database level. If oversells occur, investigate whether the application is
bypassing the constraints or whether there is a race condition in the
reservation logic.

### 7. Owner Correction Rate

**Definition:** Percentage of AI-handled transactions that the owner manually
corrects or cancels via WhatsApp.

**Data source:** Count of orders/appointments where status changes from
`confirmed` to `cancelled` within 1 hour of creation, initiated by owner.
Track via `owner_audit_log` entries.

**Formula:** `owner_corrections / total_transactions * 100`

**If threshold not met:** Interview owners to understand why they are correcting.
Common causes: wrong item heard by STT, wrong quantity, customer changed mind
after AI confirmed. Improve STT accuracy or add stricter confirmation flow.

### 8. Escalation Correctness

**Definition:** Percentage of escalations to the owner that were appropriate
(the AI correctly identified it could not handle the request).

**Data source:** Manual review of escalated calls. A human labels each
escalation as "correct" (AI should have escalated) or "incorrect" (AI could
have handled it).

**Formula:** `correct_escalations / total_escalations * 100`

**If threshold not met:** If too many false escalations: improve tool coverage
and system prompt. If too few escalations (AI tries to handle things it should
not): add explicit escalation triggers for edge cases.

### 9. P50 Response Latency

**Definition:** Median time from end of caller utterance to start of AI speech
response, measured across all conversational turns.

**Data source:** Application-level timing. Measure from STT final result
timestamp to TTS first audio byte timestamp.

**Formula:** 50th percentile of all turn latencies.

**If threshold not met:** Profile the pipeline to identify the bottleneck.
Typical breakdown: STT finalization (200-500ms), LLM inference (300-1000ms),
TTS generation (200-500ms). Optimize the slowest segment first. Consider
streaming TTS to overlap with LLM generation.

### 10. P95 Response Latency

**Definition:** 95th percentile of response latency (same measurement as P50).

**Data source:** Same as P50.

**Formula:** 95th percentile of all turn latencies.

**If threshold not met:** Investigate tail latency causes. Common: cold LLM
inference, network variability to Sarvam API, large payloads. Add timeouts
and fallback responses for slow turns.

### 11. Cost Per Connected Minute

**Definition:** Total provider cost (STT + LLM + TTS + telephony) divided by
total connected call minutes.

**Data source:** Provider billing dashboards (Sarvam, Exotel). Call duration
from `calls.duration_sec`.

**Formula:** `(sarvam_stt_cost + sarvam_llm_cost + sarvam_tts_cost + exotel_cost) / total_connected_minutes`

**If threshold not met:** Identify the most expensive component. Likely
candidates: telephony (Exotel per-minute charges), TTS (character-based
pricing). Optimize: reduce verbose AI responses, compress prompt to reduce
LLM tokens, negotiate volume pricing.

### 12. Cost Per Successful Transaction

**Definition:** Total provider cost divided by total successful transactions
(completed orders + confirmed appointments).

**Data source:** Provider billing divided by count of orders with status
`confirmed` + appointments with status `confirmed`.

**Formula:** `total_provider_cost / (confirmed_orders + confirmed_appointments)`

**If threshold not met:** Reduce call duration for transactional calls (faster
confirmation flow). Reduce enquiry-only calls that do not convert to
transactions (not actionable, but understand the ratio).

### 13. Caller Abandonment Rate

**Definition:** Percentage of calls where the caller hangs up before the AI
provides a resolution.

**Data source:** Calls where `outcome` is `dropped` or `no_action` and
`duration_sec > 10`.

**Formula:** `abandoned_calls / total_valid_calls * 100`

**If threshold not met:** Analyze at which point in the conversation callers
abandon. Common causes: too slow to respond (latency), AI not understanding
(language/accent), caller frustrated with confirmation flow. Listen to a sample
of abandoned call recordings.

### 14. Owner Satisfaction

**Definition:** Average satisfaction score from owner survey (1 = very
dissatisfied, 5 = very satisfied).

**Data source:** WhatsApp survey sent to owners after 1 week and 2 weeks of
pilot usage. Simple question: "How satisfied are you with your AI assistant?
Reply 1-5."

**Formula:** Mean of all responses.

**If threshold not met:** Conduct phone interviews with dissatisfied owners.
Understand whether the issue is quality (wrong orders), reliability (missed
calls), or unmet expectations (feature gaps).

### 15. Willingness to Pay

**Definition:** Percentage of trial users who indicate they would pay for the
service after the trial period.

**Data source:** WhatsApp survey at end of trial: "Would you pay Rs 499/month
to continue using this service? Reply YES or NO."

**Formula:** `yes_responses / total_responses * 100`

**If threshold not met:** The value proposition is not strong enough. Analyze
which business types show highest willingness. Consider: reducing price,
adding more features, targeting different business categories, or improving
call quality first.

### 16. Renewal Intent

**Definition:** Percentage of trial users who express intent to renew when
asked directly.

**Data source:** WhatsApp or phone follow-up at trial end: "Your trial ends
in 3 days. Would you like to continue?"

**Formula:** `intend_to_renew / total_contacted * 100`

**If threshold not met:** Compare with willingness-to-pay metric. If willingness
is high but renewal is low, the issue may be payment friction or timing. If
both are low, the product needs fundamental improvement before scaling.

---

## Go/No-Go Decision Framework

### Go (proceed to next stage)

All of the following must be true:
- Safety metrics (4, 5, 6) are at or better than threshold.
- At least 4 of 5 quality metrics (1, 2, 3, 7, 8) meet threshold.
- At least 1 of 2 performance metrics (9, 10) meets threshold.
- At least 1 of 3 business metrics (14, 15, 16) meets threshold.

### Conditional Go (proceed with specific remediation plan)

- Safety metrics are met.
- 2-3 quality metrics are below threshold but root cause is understood and a
  fix is identified.
- Business metrics show positive signal even if below threshold.

### No-Go (do not proceed)

- Any safety metric is violated (double-booking or oversell occurred).
- Fewer than 2 quality metrics meet threshold.
- Owner satisfaction is below 2.5.
- Zero owners express willingness to pay.

---

## Data Collection Schedule

| Week | Activity |
|------|----------|
| Week 1 | Instrument all metrics. Begin collecting automated metrics. |
| Week 2 | First manual transcript review (50 calls minimum). First owner survey. |
| Week 3 | Second transcript review. Mid-pilot owner interviews. |
| Week 4 | Final transcript review. End-of-trial survey. Go/no-go decision. |
