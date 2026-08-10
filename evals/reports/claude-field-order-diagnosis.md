# Claude model_ignores_collection_state Diagnosis

## Question
Claude went 3→8→26 on model_ignores_collection_state between unwired
(3), wired at 20 cases (8), and wired at 50 cases (12→26 scaled).
Did the BookingStateInjector make Claude worse?

## Finding
The scorer has NO "confirmation" pattern — when required_field is
"confirmation", it cannot fire model_ignores_collection_state. So
all 26 MEDIUM defects come from turns where required_field is
"date", "time", or "reason" and Claude responds by asking for name.

This is NOT a scorer false positive. It is NOT a timing issue.
Claude genuinely asks for the patient's name when the injected state
says to ask for date, time, or reason.

## Why the injector made it worse
Without the injector, Claude follows its own judgment about field order
— which sometimes matches the expected order (3 mismatches in 20 cases).

With the injector, the BookingCollection state is explicitly injected
saying `required_field: date`, but Claude ignores it and asks for name
anyway. The injector didn't change Claude's behavior — it changed what
correct behavior IS, and Claude fails it more often.

The 3→26 increase is real. The injector raised the bar (now there IS
a correct field to ask for), and Claude fails to meet it in 52% of
conversations.

## Luna comparison
Luna: 0 model_ignores_collection_state in 50 wired cases.
Luna follows the injected required_field faithfully.

## Implication
If Claude is selected, the injector's field-order guidance is not
sufficient — Claude may need a stronger constraint (e.g., the runtime
deterministically generating the question text, not just the field name,
and the LLM only translating it to natural Tamil).
