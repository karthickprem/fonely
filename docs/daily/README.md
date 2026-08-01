# Fonely Daily Engineering Log

This directory tracks Fonely's work by calendar date.

## How to use it

Create one folder per day using `YYYY-MM-DD`:

```text
docs/daily/
├── README.md
├── 2026-07-31/
│   └── STATUS.md
├── 2026-08-01/
│   └── STATUS.md
└── 2026-08-02/
    └── STATUS.md
```

Each `STATUS.md` should record:

1. **Day objective** — what we intended to achieve.
2. **Built today** — implemented artifacts, not plans.
3. **Verified today** — exact commands and observed results.
4. **Review findings** — confirmed gaps or defects.
5. **Pending work** — prioritized next tasks.
6. **Blockers/decisions** — external dependencies and founder decisions.
7. **Next-day objective** — one bounded deliverable.

## Historical and current status policy

Daily logs are append-only chronological evidence. Earlier counts, blockers, and statements should not be silently rewritten when later work supersedes them. Append a dated correction/supersession note when necessary and link to `docs/STATUS.md`, which is the authoritative current-state document.

## Reporting rules

- A model/table/comment is not a completed feature.
- Mark work complete only when its behavior and tests exist.
- Copy exact test, lint, type-check, and migration results.
- Say when PostgreSQL, Exotel, Sarvam, or WhatsApp integration was not tested.
- Never copy API keys, passwords, phone PINs, or `.env` contents into these logs.
- Distinguish `implemented`, `statically verified`, `unit tested`, `integration tested`, and `production validated`.
- Add newly discovered work to **Pending**, rather than silently expanding the current day's scope.

## Status legend

- ✅ Implemented and verified at the stated level
- 🟡 Implemented but incompletely verified
- ⏳ Pending
- 🚫 Blocked
- ⚠️ Known issue
